from __future__ import annotations
import time
import pump
import expel
import json
from dataclasses import dataclass
from typing import Tuple, Optional, Dict, List
import socket
import threading

# -----------------------------
# Reconnecting TCP Robot Client (integrated from dobotcontrol.py)
# -----------------------------
class RobotClient:
    """Robust reconnecting TCP client for dobot."""
    def __init__(
        self,
        ip: str,
        port: int,
        *,
        timeout_s: float = 2.0,
        max_retries: int = 5,
        retry_delay_s: float = 0.25,
        backoff: float = 2.0,
        add_newline: bool = False,
        keepalive: bool = True,
        recv_buf: int = 4096,
        verbose: bool = True,
    ):
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_delay_s = retry_delay_s
        self.backoff = backoff
        self.add_newline = add_newline
        self.keepalive = keepalive
        self.recv_buf = recv_buf
        self.verbose = verbose

        self._sock: Optional[socket.socket] = None
        # Re-entrant lock because request() may call connect()/close() internally.
        self._lock = threading.RLock()

    def connect(self) -> None:
        """Open a new TCP connection."""
        with self._lock:
            self.close()

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout_s)

            if self.keepalive:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except OSError:
                    pass

            s.connect((self.ip, self.port))
            self._sock = s

            if self.verbose:
                print(f"[RobotClient] Connected to {self.ip}:{self.port}")

    def close(self) -> None:
        """Close the current connection."""
        with self._lock:
            sock = self._sock
            self._sock = None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass

    def _ensure_connected(self) -> None:
        if self._sock is None:
            self.connect()

    def request(self, message: str, *, safe_to_retry: bool = True, timeout_s: Optional[float] = None) -> Optional[str]:
        """Send one command and read one response. Reconnect + retry on connection errors."""
        msg = message + ("\n" if self.add_newline else "")

        with self._lock:
            attempts = self.max_retries if safe_to_retry else 1
            delay = self.retry_delay_s

            for attempt in range(1, attempts + 1):
                try:
                    self._ensure_connected()
                    sock = self._sock
                    if sock is None:
                        raise ConnectionError("Socket is not connected")
                    prev_timeout = None
                    if timeout_s is not None:
                        try:
                            prev_timeout = sock.gettimeout()
                        except OSError:
                            prev_timeout = None
                        sock.settimeout(timeout_s)
                    sock.sendall(msg.encode("utf-8"))
                    data = sock.recv(self.recv_buf)

                    if timeout_s is not None and prev_timeout is not None:
                        try:
                            sock.settimeout(prev_timeout)
                        except OSError:
                            pass

                    if data == b"":
                        raise ConnectionResetError("Remote closed connection (empty recv).")

                    resp = data.decode("utf-8", errors="replace").strip()

                    is_di_poll = message.strip().lower().startswith("di ")
                    if self.verbose and (not is_di_poll):
                        print(f"Sent: {message}")
                        print(f"Received: {resp}")

                    return resp

                except (socket.timeout, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
                    if self.verbose:
                        print(f"[RobotClient] Error attempt {attempt}/{attempts}: {e}")

                    self.close()

                    if (not safe_to_retry) or (attempt == attempts):
                        return None

                    time.sleep(delay)
                    delay *= self.backoff

            return None

    def drain(self, *, max_reads: int = 5) -> None:
        """Drain any pending responses to avoid stale data delaying new commands."""
        if self._sock is None:
            return
        try:
            prev_timeout = self._sock.gettimeout()
            self._sock.settimeout(0.0)
            for _ in range(max_reads):
                try:
                    data = self._sock.recv(self.recv_buf)
                    if not data:
                        break
                except (BlockingIOError, socket.timeout):
                    break
        except OSError:
            pass
        finally:
            try:
                self._sock.settimeout(prev_timeout)
            except Exception:
                pass


def set_output(
    client: RobotClient,
    index: int,
    state: str,
    *,
    timeout_s: Optional[float] = None,
    safe_to_retry: bool = True
) -> Optional[str]:
    """Set digital output on the robot: state must be 'on' or 'off'."""
    if state not in ("on", "off"):
        raise ValueError("state must be 'on' or 'off'")
    client.drain()
    return client.request(f"do {index} {state}", safe_to_retry=safe_to_retry, timeout_s=timeout_s)


def get_input(client: RobotClient, index: int) -> Optional[int]:
    """Read a digital input from the robot. Returns 0/1, or None on error."""
    resp = client.request(f"di {index}", safe_to_retry=True)
    if not resp:
        return None
    parts = resp.split()
    if len(parts) >= 3 and parts[0].lower() == "di":
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None


# -----------------------------
# Robot Loader (integrated sample loading logic)
# -----------------------------
class RobotLoader:
    """Handles lipid loading operations using robot + microcontroller."""
    
    SENSOR_PORTS = {1: 7, 2: 8, 3: 6}  # line -> sensor port mapping
    VALVE_ON_PRESSURE_LIMIT_MBAR = 200.0
    VALVE_ON_AFTER_HIGH_PRESSURE_COOLDOWN_S = 8.0
    
    def __init__(
        self,
        dobot_client: RobotClient,
        ser,
        ser_secondary,
        calibarr,
        status_callback=None,
        wait_for_idle=None,
        idle_timeout_s: float = 120.0,
        idle_settle_s: float = 1.0,
        stable_flush_time_s: float = 30.0,
        stable_load_time_s: float = 6.5,
        load_flush_through_chip: bool = False,
        wash_cycles: int = 1,
        cleaning_flush_pressure_mbar: float = 70.0,
    ):
        self.dobot_client = dobot_client
        self.ser = ser
        self.ser_secondary = ser_secondary
        self.calibarr = calibarr
        self.status_callback = status_callback
        self.wait_for_idle = wait_for_idle
        self.idle_timeout_s = idle_timeout_s
        self.idle_settle_s = idle_settle_s
        self.stable_flush_time_s = float(stable_flush_time_s)
        self.stable_load_time_s = float(stable_load_time_s)
        self.load_flush_through_chip = bool(load_flush_through_chip)
        self.wash_cycles = max(1, int(wash_cycles))
        self.cleaning_flush_pressure_mbar = float(cleaning_flush_pressure_mbar)
        self._last_input_read: Dict[int, float] = {}
        self._input_cache: Dict[int, Optional[int]] = {}
        self._input_min_interval_s = 0.3
        self._sensor_log_interval_s = 30.0
        self._last_sensor_log_t = 0.0
        self._sensor_snapshot: Dict[int, Optional[int]] = {6: None, 7: None, 8: None}
        # Shared microcontroller serial I/O (servo + pump) must be serialized across threads.
        self._mc_io_lock = threading.RLock()
        self._valve_state_lock = threading.RLock()
        self._dobot_valve_state: Dict[int, Optional[str]] = {1: None, 2: None, 3: None}
        self._line_pressure_lock = threading.RLock()
        self._line_pressure_state: Dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0}
        self._last_high_pressure_ts: Dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0}

        self._last_status_time = 0.0
        self._status_min_interval_s = 0.5

    def _status(self, message: str) -> None:
        if not self.status_callback:
            return
        now = time.monotonic()
        if (now - self._last_status_time) < self._status_min_interval_s:
            return
        self._last_status_time = now
        try:
            self.status_callback(message)
        except Exception:
            pass

    def _wait_for_system_idle(self, reason: str) -> None:
        """Block until the dobot system is idle before critical valve/pressure actions."""
        if not self.wait_for_idle:
            return
        if not self.wait_for_idle(self.idle_timeout_s):
            raise RuntimeError(f"Robot not idle before {reason}")
        if self.idle_settle_s > 0:
            time.sleep(self.idle_settle_s)

    def _set_servo_angle(self, ser, servo_idx: int, angle: float) -> None:
        # Secondary MCU wiring compensation: logical 8/9 are swapped physically.
        if ser is self.ser_secondary:
            if int(servo_idx) == 8:
                servo_idx = 9
            elif int(servo_idx) == 9:
                servo_idx = 8
        with self._mc_io_lock:
            expel.set_servo_angle(ser, servo_idx, angle)

    def _set_pressure(self, channel: int, value: float) -> None:
        with self._mc_io_lock:
            pump.set_pressure(channel, value, self.calibarr)

    def _get_sensor_data(self, channel: int):
        with self._mc_io_lock:
            return pump.get_sensor_data(channel)

    def _set_dobot_valve(
        self,
        line: int,
        state: str,
        *,
        settle_s: float = 0.25,
        retries: int = 5,
        require_idle: bool = True,
    ) -> None:
        """Set dobot output for the line and wait for actuation before pressure."""
        if require_idle:
            self._wait_for_system_idle("dobot valve change")
        # Safety interlock: only force low pressure before valve ON when current pressure is high.
        if state == "on":
            with self._line_pressure_lock:
                current_p = float(self._line_pressure_state.get(int(line), 0.0))
                last_high_t = float(self._last_high_pressure_ts.get(int(line), 0.0))
            now_t = time.monotonic()
            elapsed = now_t - last_high_t if last_high_t > 0 else float("inf")
            if elapsed < self.VALVE_ON_AFTER_HIGH_PRESSURE_COOLDOWN_S:
                remaining = self.VALVE_ON_AFTER_HIGH_PRESSURE_COOLDOWN_S - elapsed
                self._status(
                    f"Line {line}: Safety cooldown {remaining:.1f}s after high pressure before valve ON"
                )
                print(
                    f"[RobotLoader] Safety interlock: line {line} waiting {remaining:.1f}s "
                    f"after high-pressure step before valve ON"
                )
                self._set_line_pressure(line, 0)
                time.sleep(max(0.0, remaining))
                with self._line_pressure_lock:
                    current_p = float(self._line_pressure_state.get(int(line), 0.0))
            if current_p > self.VALVE_ON_PRESSURE_LIMIT_MBAR:
                self._status(
                    f"Line {line}: Safety interlock setting pressure to 0 mbar before valve ON "
                    f"(current {current_p:.1f} mbar)"
                )
                print(
                    f"[RobotLoader] Safety interlock: line {line} pressure {current_p:.1f} mbar "
                    f"> {self.VALVE_ON_PRESSURE_LIMIT_MBAR:.0f}, setting 0 mbar before valve ON"
                )
                self._set_line_pressure(line, 0)
                time.sleep(0.1)
        last_resp = None
        start_t = time.monotonic()
        for attempt in range(1, retries + 1):
            last_resp = set_output(
                self.dobot_client,
                line + 8,
                state,
                timeout_s=0.5,
                safe_to_retry=True,
            )
            if last_resp is not None:
                time.sleep(settle_s)
                with self._valve_state_lock:
                    self._dobot_valve_state[int(line)] = state
                elapsed = time.monotonic() - start_t
                print(f"[RobotLoader] Dobot valve L{line} -> {state} ack in {elapsed:.3f}s")
                return
            backoff_s = min(0.15 * attempt, 0.75)
            print(
                f"[RobotLoader] Dobot valve L{line} -> {state} no ack "
                f"(attempt {attempt}/{retries}), retrying in {backoff_s:.2f}s"
            )
            time.sleep(backoff_s)
        raise RuntimeError(f"Failed to set dobot valve line {line} to {state}. Last response: {last_resp}")

    def _get_input_throttled(self, index: int) -> Optional[int]:
        """Throttle dobot input reads to avoid spamming the queue."""
        now = time.monotonic()
        last = self._last_input_read.get(index, 0.0)
        if (now - last) < self._input_min_interval_s:
            return self._input_cache.get(index)

        val = get_input(self.dobot_client, index)
        self._last_input_read[index] = now
        self._input_cache[index] = val
        if index in self._sensor_snapshot:
            self._sensor_snapshot[index] = val
            if (now - self._last_sensor_log_t) >= self._sensor_log_interval_s:
                self._last_sensor_log_t = now
                d6 = self._sensor_snapshot.get(6)
                d7 = self._sensor_snapshot.get(7)
                d8 = self._sensor_snapshot.get(8)
                print(f"[RobotLoader] Sensor states: DI6={d6} DI7={d7} DI8={d8}")
        return val

    def _get_dobot_valve_state(self, line: int) -> Optional[str]:
        with self._valve_state_lock:
            return self._dobot_valve_state.get(int(line))

    def _set_line_pressure(self, line: int, value: float, *, reason: str = "") -> None:
        """Set pressure on a line, track setpoint, and log changes for debugging."""
        val = float(value)
        with self._line_pressure_lock:
            prev = float(self._line_pressure_state.get(int(line), 0.0))
        reason_txt = f" | reason={reason}" if reason else ""
        print(f"[RobotLoader] Pressure setpoint L{line}: {prev:.1f} -> {val:.1f} mbar{reason_txt}")
        self._set_pressure(line + 1, val)
        with self._line_pressure_lock:
            self._line_pressure_state[int(line)] = val
            if val > self.VALVE_ON_PRESSURE_LIMIT_MBAR:
                self._last_high_pressure_ts[int(line)] = time.monotonic()

    def set_plate_manager(self, plate_manager: PlateManager):
        self.plate_manager = plate_manager

    def prime_ethanol_to_tjunction(self, line: int) -> None:
        """Prime ethanol line to T-junction."""
        print(f"[RobotLoader] Priming ethanol to T-junction for line {line}")
        self._status(f"Line {line}: Prime ethanol to T-junction")

        # Safety handshake before high-pressure priming:
        # enforce OFF with longer settle, force 0 mbar, then assert OFF again.
        self._set_dobot_valve(line, "off", settle_s=1.0)
        self._set_line_pressure(line, 0)
        time.sleep(0.3)
        self._set_dobot_valve(line, "off", settle_s=0.4, require_idle=False)

        self._set_servo_angle(self.ser, line, 40)  # Close to dobot
        print(f"[Servo] Close to chip: servo {line + 3} (line {line}) reason=ethanol_prime")
        self._set_servo_angle(self.ser, line + 3, 40)  # Close to chip
        
        try:
            self._wait_for_system_idle("pressure ramp (ethanol prime)")
            self._set_line_pressure(line, 2000)
            time.sleep(15)
        finally:
            # Safety: always cut pressure after T-junction priming.
            self._set_line_pressure(line, 0)
            time.sleep(0.5)  # Settle time to ensure pressure fully released
        print(f"[RobotLoader] ✓ Ethanol prime complete for line {line}")

    def flush_main_line_with_air(
        self,
        line: int,
        *,
        flush_through_chip: bool = False,
        precheck_delay_s: float = 5.0,
        post_stable_hold_s: float = 0.0,
        require_idle: bool = True,
        air_pressure_mbar: Optional[float] = None,
    ) -> None:
        """Flush main line with air until fluid sensor detects stable air."""
        print(f"[RobotLoader] Flushing main line with air for line {line}")
        self._status(f"Line {line}: Air flush start")

        # Extra transition guard after any preceding high-pressure phase:
        # enforce 0 mbar before opening dobot valve to the line.
        self._set_line_pressure(line, 0, reason="pre_flush_valve_on")
        time.sleep(0.3)

        flush_pressure = float(air_pressure_mbar) if air_pressure_mbar is not None else 70.0
        if flush_pressure < 0:
            flush_pressure = 0.0

        self._set_servo_angle(self.ser, line, 125)  # Close to sensors
        self._set_dobot_valve(line, "on", require_idle=require_idle)
        pressure_applied = False
        if flush_through_chip:
            self._set_servo_angle(self.ser, line + 3, 125)  # Close to waste
            # Start air push while waste is closed so transition has active airflow.
            self._set_line_pressure(line, flush_pressure, reason="air_flush_through_chip_transition")
            pressure_applied = True
            time.sleep(7)
            self._set_servo_angle(self.ser, line + 3, 80)  # Open to both
        else:
            print(f"[Servo] Close to chip: servo {line + 3} (line {line}) reason=air_flush_not_through_chip")
            self._set_servo_angle(self.ser, line + 3, 40)  # Close to chip
        
        if require_idle:
            self._wait_for_system_idle("pressure ramp (air flush)")
        if not pressure_applied:
            self._set_line_pressure(line, flush_pressure)
        if precheck_delay_s > 0:
            print(f"[RobotLoader] Air flush pre-check delay: {precheck_delay_s:.0f}s")
            time.sleep(precheck_delay_s)
        
        required_stable = float(self.stable_flush_time_s)
        interval = 0.1
        stable_time = 0
        sensor_port = self.SENSOR_PORTS[line]
        
        while True:
            fluid = self._get_input_throttled(sensor_port)
            if fluid == 1:
                stable_time += interval
            else:
                stable_time = 0
            
            if stable_time >= required_stable:
                break
            
            time.sleep(interval)

        if post_stable_hold_s > 0:
            print(f"[RobotLoader] Air stable on line {line}; holding extra {post_stable_hold_s:.0f}s before stop")
            time.sleep(post_stable_hold_s)
        
        self._set_line_pressure(line, 0)
        time.sleep(0.5)  # Settle time to ensure pressure fully released
        self._status(f"Line {line}: Air flush complete")
        print(f"[RobotLoader] ✓ Air flush complete for line {line} (air stable {required_stable}s)")

    def load_sample(self, line: int) -> None:
        """Load sample into line."""
        print(f"[RobotLoader] >>> LOADING SAMPLE: Line {line}")
        self._status(f"Line {line}: Set valves for loading")
        
        self._set_servo_angle(self.ser, line, 125)  # Close to sensors
        print(f"[Servo] Close to chip: servo {line + 3} (line {line}) reason=load_sample")
        self._set_servo_angle(self.ser, line + 3, 40)  # Close to chip
        self._set_dobot_valve(line, "on")
        self._status(f"Line {line}: Apply pressure 45 mbar")
        self._wait_for_system_idle("pressure ramp (load sample)")
        self._set_line_pressure(line, 100)
        
        required_stable = float(self.stable_load_time_s)
        interval = 0.1
        stable_time = 0.0
        air_stable_time = 0.0
        passed_sensor = False
        sensor_port = self.SENSOR_PORTS[line]
        load_start_t = time.monotonic()
        escalated_pressure = False
        
        print(f"[RobotLoader] Waiting for fluid detection (need {required_stable}s stable)")
        while True:
            fluid = self._get_input_throttled(sensor_port)

            if not passed_sensor:
                if (not escalated_pressure) and ((time.monotonic() - load_start_t) >= 45.0):
                    print(f"[RobotLoader] Load not detected after 45s on line {line} - increasing pressure to 200 mbar")
                    self._status(f"Line {line}: No fluid yet at 45s, increase pressure to 200 mbar")
                    self._wait_for_system_idle("pressure ramp (load sample escalate)")
                    self._set_line_pressure(line, 200)
                    escalated_pressure = True
                if fluid == 0:
                    stable_time += interval
                else:
                    stable_time = 0.0

                if stable_time >= required_stable:
                    passed_sensor = True
                    print(f"[RobotLoader] ✓ Fluid detected stable for {required_stable}s; waiting for sensor to return to air")
                    self._status(f"Line {line}: Fluid stable {required_stable}s; waiting for pass-through")
            else:
                if fluid == 1:
                    air_stable_time += interval
                else:
                    air_stable_time = 0.0

                if air_stable_time >= 1.0:
                    print(f"[RobotLoader] ✓ Sensor returned to air for 1.0s; stopping load on line {line}")
                    break
            
            time.sleep(interval)
        
        self._set_line_pressure(line, 0)
        time.sleep(0.5)  # Settle time after stopping pump
        self._status(f"Line {line}: Stop pressure, reset valves")
        self._set_dobot_valve(line, "off")  # Valve OFF after loading complete
        self._set_servo_angle(self.ser, line, 40)  # Close to dobot
        self._set_servo_angle(self.ser, line + 3, 125)  # Close to waste
        print(f"[RobotLoader] <<< LOADING SAMPLE COMPLETE: Line {line}\n")

    def wash_tubing(
        self,
        line: int,
        target_vol: float = 200,
        *,
        flush_through_chip: bool = False,
        require_idle: bool = True,
    ) -> None:
        """Wash tubing with ethanol."""
        print(f"[RobotLoader] Washing tubing for line {line}, target volume: {target_vol}µL")
        
        sensorcorr = [[0,0,0,1.0897,-1.2766],[0.2673,-0.8813,1.3205,1.1869,-0.1],[0.2673,-0.8813,1.3205,1.1869,0],[0.2673,-0.8813,1.3205,1.1869,-0.1]]
        
        channel = line + 1
        vol = 0
        
        self._set_servo_angle(self.ser, line, 80)  # Open to both
        if flush_through_chip:
            self._set_servo_angle(self.ser, line + 3, 125)  # Close to waste
            time.sleep(7)
            self._set_servo_angle(self.ser, line + 3, 80)  # Open to both
        else:
            self._set_servo_angle(self.ser, line + 3, 80)  # Open to both
        self._set_dobot_valve(line, "off", require_idle=require_idle)
        print("Close valve-direct to reservoir")
        if self.ser_secondary:
            secondary_servo_by_line = {1: 7, 2: 8, 3: 9}
            secondary_servo = secondary_servo_by_line.get(int(line))
            if secondary_servo is not None:
                try:
                    self._set_servo_angle(self.ser_secondary, secondary_servo, 40)  # Close to chip
                except Exception as e:
                    print(f"[RobotLoader] Warning: secondary servo {secondary_servo} close failed: {e}")

        if require_idle:
            self._wait_for_system_idle("pressure ramp (wash tubing)")
        self._set_line_pressure(line, 2000)
        last_pressure_reassert_t = time.monotonic()
        
        last_time = time.time()
        print_interval = 0.0
        read_count = 0
        while vol < target_vol:
            now_mono = time.monotonic()
            if (now_mono - last_pressure_reassert_t) >= 20.0:
                self._set_line_pressure(line, 2000, reason="wash_reassert_20s")
                last_pressure_reassert_t = now_mono
            fr_raw, err = self._get_sensor_data(channel)
            if err:
                time.sleep(0.1)
                continue
            read_count += 1
            
            # Debug first few reads
            if read_count <= 5:
                poly = sensorcorr[channel-1]
                print(f"[RobotLoader] L{line} Ch{channel} Read #{read_count}: raw={fr_raw:.6f}, err={err}, poly={poly}")
            
            fr = sensorcorr[channel-1][0]*(fr_raw**4) + sensorcorr[channel-1][1]*(fr_raw**3) + sensorcorr[channel-1][2]*(fr_raw**2) + (sensorcorr[channel-1][3])*(fr_raw) + sensorcorr[channel-1][4]
            if fr < 0:
                fr = 0
            interval = time.time() - last_time
            last_time = time.time()
            vol += (fr * interval / 60)
            remaining = max(target_vol - vol, 0.0)
            print_interval += interval
            if print_interval >= 0.5:
                print(f"[RobotLoader] L{line} Wash: {vol:.2f}/{target_vol:.1f} µL | Flow: {fr:.3f} µL/min (raw: {fr_raw:.6f}) | Remaining: {remaining:.2f} µL")
                print_interval = 0.0
            self._status(f"Wash L{line}: {vol:.1f}/{target_vol:.1f} µL (remaining {remaining:.1f} µL)")
            time.sleep(0.1)
        
        self._set_line_pressure(line, 0)
        if self.ser_secondary:
            secondary_servo_by_line = {1: 7, 2: 8, 3: 9}
            secondary_servo = secondary_servo_by_line.get(int(line))
            if secondary_servo is not None:
                try:
                    self._set_servo_angle(self.ser_secondary, secondary_servo, 80)  # Open to both
                except Exception as e:
                    print(f"[RobotLoader] Warning: secondary servo {secondary_servo} open failed: {e}")
        time.sleep(3)
        print(f"[Servo] Close to chip: servo {line + 3} (line {line}) reason=wash_complete")
        self._set_servo_angle(self.ser, line + 3, 40)  # Close to chip
        print(f"[RobotLoader] Wash complete for line {line}")

    def full_load_sequence(self, line: int) -> None:
        """Execute full loading sequence: flush air -> prime ethanol -> load sample."""
        self.flush_main_line_with_air(line)
        self.prime_ethanol_to_tjunction(line)
        self.load_sample(line)

    def full_clean_sequence(
        self,
        line: int,
        wash_volume_ul: float = 200,
        flush_through_chip: bool = False,
        on_first_wash_complete=None,
    ) -> None:
        """Execute full cleaning sequence: flush air -> wash -> flush air."""
        # During parallel line cleaning we intentionally do not enforce global
        # robot-idle guards for each line; dobot I/O is already serialized by client lock.
        self.flush_main_line_with_air(
            line,
            flush_through_chip=flush_through_chip,
            require_idle=False,
            air_pressure_mbar=self.cleaning_flush_pressure_mbar,
        )
        cycles = max(1, int(getattr(self, "wash_cycles", 1)))
        for cycle_idx in range(cycles):
            if cycles > 1:
                print(f"[RobotLoader] Clean cycle {cycle_idx + 1}/{cycles} for line {line}")
            self.wash_tubing(
                line,
                target_vol=float(wash_volume_ul),
                flush_through_chip=flush_through_chip,
                require_idle=False,
            )
            if cycle_idx == 0 and callable(on_first_wash_complete):
                try:
                    on_first_wash_complete()
                except Exception as e:
                    print(f"[RobotLoader] Warning: on_first_wash_complete callback failed for line {line}: {e}")
            # Post-wash flush: delay before checking for stable air.
            self.flush_main_line_with_air(
                line,
                flush_through_chip=flush_through_chip,
                precheck_delay_s=5.0,
                require_idle=False,
                air_pressure_mbar=self.cleaning_flush_pressure_mbar,
            )

def send_abs_move(client: RobotClient, x: float, y: float, z: float, r: float, move_retries: int = 5) -> Optional[str]:
    for _ in range(move_retries):
        resp = client.request(f"movj {x} {y} {z} {r}", safe_to_retry=False)
        if resp is not None:
            return resp
        client.close()
        time.sleep(0.2)
    return None

@dataclass(frozen=True)
class Pose4:
    x: float
    y: float
    z: float
    r: float

    @staticmethod
    def from_list(vals: List[float]) -> "Pose4":
        return Pose4(float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3]))

@dataclass(frozen=True)
class PlateLandmarks:
    TL: Dict[str, Pose4]
    TR: Dict[str, Pose4]
    C: Dict[str, Pose4]
    BL: Dict[str, Pose4]
    BR: Dict[str, Pose4]

class PlateManager:
    PLATE_SPECS = {
        0: {"rows": 3, "cols": 10, "measured_center": (2, 5)},
        1: {"rows": 5, "cols": 3, "measured_center": (3, 2)},
        2: {"rows": 5, "cols": 3, "measured_center": (3, 2)},
        3: {"rows": 5, "cols": 3, "measured_center": (3, 2)},
    }

    def __init__(self, landmarks_by_plate: Dict[int, PlateLandmarks]):
        self._plates = landmarks_by_plate
        self._mean_r = {}
        for plate_idx, lm in self._plates.items():
            for mode in ("enter", "lock"):
                poses = [lm.TL[mode], lm.TR[mode], lm.C[mode], lm.BL[mode], lm.BR[mode]]
                self._mean_r[(plate_idx, mode)] = sum(p.r for p in poses) / len(poses)
        self._mean_r[(0, "lock")] = self._mean_r[(0, "enter")]

    @staticmethod
    def _bilerp_xyz(u: float, v: float, TL: Pose4, TR: Pose4, BL: Pose4, BR: Pose4) -> Tuple[float, float, float]:
        x = (1 - u) * (1 - v) * TL.x + u * (1 - v) * TR.x + (1 - u) * v * BL.x + u * v * BR.x
        y = (1 - u) * (1 - v) * TL.y + u * (1 - v) * TR.y + (1 - u) * v * BL.y + u * v * BR.y
        z = (1 - u) * (1 - v) * TL.z + u * (1 - v) * TR.z + (1 - u) * v * BL.z + u * v * BR.z
        return (x, y, z)

    def get_pose(self, plate: int, row: int, col: int, mode: str = "enter") -> Tuple[float, float, float, float]:
        rows = self.PLATE_SPECS[plate]["rows"]
        cols = self.PLATE_SPECS[plate]["cols"]
        measured_center = self.PLATE_SPECS[plate]["measured_center"]

        u = (col - 1) / (cols - 1) if cols > 1 else 0.0
        v = (row - 1) / (rows - 1) if rows > 1 else 0.0

        TL, TR, BL, BR, C = (
            self._plates[plate].TL[mode],
            self._plates[plate].TR[mode],
            self._plates[plate].BL[mode],
            self._plates[plate].BR[mode],
            self._plates[plate].C[mode],
        )

        P_bil = self._bilerp_xyz(u, v, TL, TR, BL, BR)
        cu = (measured_center[1] - 1) / (cols - 1) if cols > 1 else 0.0
        cv = (measured_center[0] - 1) / (rows - 1) if rows > 1 else 0.0
        P_bil_center = self._bilerp_xyz(cu, cv, TL, TR, BL, BR)

        offset = (C.x - P_bil_center[0], C.y - P_bil_center[1], C.z - P_bil_center[2])
        dist = ((u - cu) ** 2 + (v - cv) ** 2) ** 0.5
        falloff = max(0.0, 1.0 - dist)

        x = P_bil[0] + falloff * offset[0]
        y = P_bil[1] + falloff * offset[1]
        z = P_bil[2] + falloff * offset[2]
        r = self._mean_r[(plate, mode)]
        return (x, y, z, r)

    @classmethod
    def from_json(cls, filename: str) -> "PlateManager":
        with open(filename, "r") as f:
            data = json.load(f)

        name_to_pose = {}
        for entry in data:
            name = str(entry.get("name", ""))
            coord = entry.get("coordinate", None)
            if name and coord:
                name_to_pose[name] = Pose4.from_list(coord[:4])

        def P(n: int) -> Pose4:
            return name_to_pose[f"P{n}"]

        landmarks_by_plate = {}
        for plate in range(4):
            base = plate * 10
            TL = {"enter": P(base + 1), "lock": P(base + 2)}
            TR = {"enter": P(base + 3), "lock": P(base + 4)}
            C = {"enter": P(base + 5), "lock": P(base + 6)}
            BL = {"enter": P(base + 7), "lock": P(base + 8)}
            BR = {"enter": P(base + 9), "lock": P(base + 10)}
            landmarks_by_plate[plate] = PlateLandmarks(TL=TL, TR=TR, C=C, BL=BL, BR=BR)

        return cls(landmarks_by_plate)
