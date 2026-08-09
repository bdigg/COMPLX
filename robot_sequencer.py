import threading
import queue
import time
from typing import Optional, List
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from robot_loader import PlateManager, send_abs_move, set_output, RobotClient

@dataclass
class RobotTask:
    """Single robot task."""
    task_type: str  # "load_lipid", "clean_all", "move_to_rest"
    line: Optional[int] = None
    lines: Optional[List[int]] = None
    clean_volume_ul: Optional[float] = None
    clean_through_chip: Optional[bool] = None
    plate: Optional[int] = None
    row: Optional[int] = None
    col: Optional[int] = None
    lipid_name: Optional[str] = None

class RobotSequencer:
    """Non-blocking robot task queue."""
    
    HOLDING_POSITIONS = {
        1: (0, 2, 2),  # plate, row, col
        2: (0, 2, 5),
        3: (0, 2, 8)
    }
    ARM_SETTLE_AFTER_LOAD_S = 20.0
    
    def __init__(self, lipid_tracker, dobot_client=None):
        self.lipid_tracker = lipid_tracker
        self.dobot_client = dobot_client
        self.task_queue: queue.Queue = queue.Queue()
        self.current_task: Optional[RobotTask] = None
        self.is_running = False
        self.robot_thread = None
        self.plate_manager = None
        self.robot_loader = None
        self.status_callback = None
        self.error_callback = None
        self.line_status_callback = None
        self._last_task_error = None
        self._error_lock = threading.Lock()
        self._load_sequence_lock = threading.Lock()
        self._manual_pose: Optional[tuple[float, float, float, float]] = None
        self.remove_stoppers_enabled: bool = False

    def _status(self, message: str) -> None:
        if not self.status_callback:
            return
        try:
            self.status_callback(message)
        except Exception:
            pass

    def set_status_callback(self, callback):
        self.status_callback = callback

    def set_error_callback(self, callback):
        self.error_callback = callback

    def set_line_status_callback(self, callback):
        self.line_status_callback = callback

    def _line_status(self, line: Optional[int], message: str) -> None:
        if line is None or not self.line_status_callback:
            return
        try:
            self.line_status_callback(int(line), message)
        except Exception:
            pass

    def set_dobot_client(self, dobot_client):
        self.dobot_client = dobot_client

    def set_plate_calibration(self, calib_file: str):
        self.plate_manager = PlateManager.from_json(calib_file)

    def set_robot_loader(self, robot_loader):
        self.robot_loader = robot_loader

    def set_remove_stoppers_enabled(self, enabled: bool) -> None:
        self.remove_stoppers_enabled = bool(enabled)

    def queue_load_lipid(self, line: int, plate: int, row: int, col: int, lipid_name: str) -> None:
        """Queue a lipid load task."""
        task = RobotTask(
            task_type="load_lipid",
            line=line,
            plate=plate,
            row=row,
            col=col,
            lipid_name=lipid_name
        )
        self.task_queue.put(task)
        self._status(f"Queued load: Line {line} from P{plate} {row},{col}")
        self._line_status(line, "Queued for loading")

    def queue_clean_line(
        self,
        line: int,
        clean_volume_ul: Optional[float] = None,
        clean_through_chip: Optional[bool] = None,
    ) -> None:
        """Queue clean task for a specific line."""
        self.task_queue.put(
            RobotTask(
                task_type="clean_all",
                line=line,
                clean_volume_ul=clean_volume_ul,
                clean_through_chip=clean_through_chip,
            )
        )
        self._line_status(line, "Queued for cleaning")

    def queue_clean_lines(
        self,
        lines: List[int],
        clean_volume_ul: Optional[float] = None,
        clean_through_chip: Optional[bool] = None,
    ) -> None:
        """Queue one batched clean task for multiple lines (clean in parallel)."""
        uniq = sorted({int(l) for l in lines if int(l) in (1, 2, 3)})
        if not uniq:
            return
        self.task_queue.put(
            RobotTask(
                task_type="clean_batch",
                lines=uniq,
                clean_volume_ul=clean_volume_ul,
                clean_through_chip=clean_through_chip,
            )
        )
        for line in uniq:
            self._line_status(line, "Queued for cleaning")

    def queue_clean_all(self) -> None:
        """Queue clean-all task for all 3 lines."""
        for line in [1, 2, 3]:
            self.task_queue.put(RobotTask(task_type="clean_all", line=line))

    def queue_move_to_rest(self, line: int) -> None:
        """Queue move-to-holding-position task."""
        self.task_queue.put(RobotTask(task_type="move_to_rest", line=line))

    def start(self) -> None:
        """Start background task processor."""
        if self.is_running:
            return
        self.is_running = True
        self.robot_thread = threading.Thread(target=self._run, daemon=True)
        self.robot_thread.start()

    def stop(self) -> None:
        """Stop background task processor."""
        self.is_running = False
        if self.robot_thread:
            self.robot_thread.join(timeout=5)

    def is_busy(self) -> bool:
        """Check if robot is currently processing a task."""
        return self.current_task is not None or not self.task_queue.empty()

    def wait_until_idle(self, timeout_s: float = 300.0) -> bool:
        """Block until robot completes all pending tasks. Returns True if idle, False if timeout."""
        import time
        start = time.time()
        while self.is_busy():
            if time.time() - start > timeout_s:
                return False
            time.sleep(0.1)
        return True

    def pop_last_error(self) -> Optional[str]:
        with self._error_lock:
            err = self._last_task_error
            self._last_task_error = None
            return err

    def is_robot_thread(self) -> bool:
        """Return True if called from the robot worker thread."""
        return threading.current_thread() is self.robot_thread

    def _run(self) -> None:
        """Background task processor."""
        while self.is_running:
            try:
                task = self.task_queue.get(timeout=1)
            except queue.Empty:
                continue
            
            try:
                self.current_task = task
                with self._error_lock:
                    self._last_task_error = None
                
                # Collect all pending tasks of the same type for batching
                pending_tasks = [task]
                while True:
                    try:
                        next_task = self.task_queue.get_nowait()
                        if next_task.task_type == task.task_type:
                            pending_tasks.append(next_task)
                        else:
                            # Put non-matching tasks back in queue
                            self.task_queue.put(next_task)
                            break
                    except queue.Empty:
                        break
                
                if task.task_type == "load_lipid" and len(pending_tasks) > 1:
                    print(f"[RobotSequencer] Sequentially loading {len(pending_tasks)} lines")
                    for t in pending_tasks:
                        self._execute_task(t)
                elif task.task_type == "clean_all" and len(pending_tasks) > 1:
                    print(f"[RobotSequencer] Sequentially cleaning {len(pending_tasks)} lines")
                    self._execute_batch_cleans(pending_tasks)
                else:
                    self._execute_task(task)
            except Exception as e:
                print(f"Robot task error: {e}")
                self._status(f"Robot task error: {e}")
                with self._error_lock:
                    self._last_task_error = str(e)
                fault = self._build_fault(task, e)
                if fault and self.error_callback:
                    try:
                        self.error_callback(fault)
                    except Exception:
                        pass
            finally:
                self.current_task = None

    def _is_dobot_comm_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = (
            "timed out",
            "timeout",
            "connection",
            "broken pipe",
            "reset",
            "empty recv",
            "move to",
            "failed",
            "no ack",
        )
        return any(m in msg for m in markers)

    def _build_fault(self, task: RobotTask, exc: Exception) -> Optional[dict]:
        if not task:
            return None
        # Only treat load/move dobot-path failures as recoverable dobot faults.
        if task.task_type not in ("load_lipid", "move_to_rest"):
            return None
        if not self._is_dobot_comm_error(exc):
            return None
        return {
            "code": "DOBOT_UNRESPONSIVE",
            "message": str(exc),
            "task_type": task.task_type,
            "line": task.line,
            "plate": task.plate,
            "row": task.row,
            "col": task.col,
            "lipid_name": task.lipid_name,
            "timestamp": time.time(),
        }

    def _execute_task(self, task: RobotTask) -> None:
        """Execute a single robot task."""
        if not self.dobot_client:
            print(f"[RobotSequencer] Simulating: {task}")
            return

        if task.task_type == "load_lipid":
            if not self.plate_manager:
                raise RuntimeError("Plate calibration not set")
            
            if not self.robot_loader:
                raise RuntimeError("Robot loader not initialized - cannot execute load sequence")
            # Hard serialization guard: one full line load cycle at a time.
            with self._load_sequence_lock:
                # For single load, run pre-dobot steps then dobot+load in order.
                self._line_status(task.line, "Pre-load setup")
                self._execute_pre_dobot_steps(task)
                self._line_status(task.line, "Loading")
                self._execute_dobot_and_load(task)
                # Do not start next line sequence until the arm has physically settled.
                if self.ARM_SETTLE_AFTER_LOAD_S > 0:
                    self._status(
                        f"Line {task.line}: waiting {self.ARM_SETTLE_AFTER_LOAD_S:.1f}s for arm settle"
                    )
                    time.sleep(self.ARM_SETTLE_AFTER_LOAD_S)
                self._line_status(task.line, "Loaded")
        
        elif task.task_type == "clean_all":
            print(f"[Robot] Cleaning line {task.line}")
            if self.robot_loader:
                self._line_status(task.line, "Cleaning")
                cleared_once = {"done": False}

                def _mark_line_empty_after_first_wash():
                    if cleared_once["done"]:
                        return
                    cleared_once["done"] = True
                    self.lipid_tracker.clear_line(task.line)
                    self._line_status(task.line, "Empty/Clean (after wash 1)")
                    self._status(f"Line {task.line}: marked empty/clean after first wash")

                if task.clean_volume_ul is None:
                    self.robot_loader.full_clean_sequence(
                        task.line,
                        flush_through_chip=bool(task.clean_through_chip),
                        on_first_wash_complete=_mark_line_empty_after_first_wash,
                    )
                else:
                    self.robot_loader.full_clean_sequence(
                        task.line,
                        wash_volume_ul=float(task.clean_volume_ul),
                        flush_through_chip=bool(task.clean_through_chip),
                        on_first_wash_complete=_mark_line_empty_after_first_wash,
                    )
                self._line_status(task.line, "Clean complete")
            else:
                self.lipid_tracker.clear_line(task.line)
                self._line_status(task.line, "Clean complete")

        elif task.task_type == "clean_batch":
            lines = list(task.lines or [])
            tasks = [
                RobotTask(
                    task_type="clean_all",
                    line=line,
                    clean_volume_ul=task.clean_volume_ul,
                    clean_through_chip=task.clean_through_chip,
                )
                for line in lines
            ]
            self._execute_batch_cleans(tasks)
        
        elif task.task_type == "move_to_rest":
            self._move_to_holding(task.line)

    def _execute_batch_loads(self, tasks: List[RobotTask]) -> None:
        """Execute pre-dobot steps in parallel for multiple tasks, then dobot operations sequentially."""
        print(f"[RobotSequencer] Starting batch parallel pre-dobot steps for {len(tasks)} tasks")
        self._status(f"Preparing {len(tasks)} samples: priming ethanol and flushing lines")
        
        # Phase 1: Parallelize pre-dobot steps (prime ethanol + air flush)
        completion_status = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for task in tasks:
                future = executor.submit(self._execute_pre_dobot_steps, task)
                futures.append((task, future))
            
            # Wait for all pre-dobot steps to complete with verification
            for task, future in futures:
                try:
                    future.result()
                    completion_status[task.line] = True
                    print(f"[RobotSequencer] ✓ Pre-dobot steps COMPLETE for line {task.line}")
                    self._status(f"Line {task.line}: Priming + flushing complete, ready for loading")
                except Exception as e:
                    print(f"[RobotSequencer] ✗ Pre-dobot steps FAILED for line {task.line}: {e}")
                    self._status(f"Line {task.line}: ERROR in pre-dobot setup")
                    raise
        
        # Verification barrier: ensure ALL pre-dobot steps completed
        if len(completion_status) != len(tasks):
            raise RuntimeError(f"Pre-dobot synchronization failed: {len(completion_status)}/{len(tasks)} complete")
        
        print(f"[RobotSequencer] ✓ ALL PRE-DOBOT STEPS COMPLETE - synchronization barrier passed")
        self._status(f"All {len(tasks)} samples ready - beginning dobot loading sequence")
        
        # Phase 2: Sequential dobot operations and loading
        print(f"[RobotSequencer] >>> PHASE 2: Sequential dobot operations and loading BEGIN")
        for idx, task in enumerate(tasks, 1):
            self.current_task = task
            try:
                print(f"[RobotSequencer] Loading line {task.line} ({idx}/{len(tasks)})")
                self._status(f"Loading sample {idx}/{len(tasks)}: Line {task.line}")
                self._execute_dobot_and_load(task)
                print(f"[RobotSequencer] ✓ Line {task.line} loading complete")
            except Exception as e:
                print(f"Robot dobot+load error for line {task.line}: {e}")
                self._status(f"Line {task.line}: ERROR during loading")
                raise
            finally:
                self.current_task = None
        
        print(f"[RobotSequencer] ✓ ALL DOBOT+LOAD OPERATIONS COMPLETE")

    def _execute_batch_cleans(self, tasks: List[RobotTask]) -> None:
        """Execute clean sequences in parallel for multiple lines."""
        if not self.robot_loader:
            # Fallback: clear line state if no loader available
            for task in tasks:
                self.lipid_tracker.clear_line(task.line)
            return

        print(f"[RobotSequencer] Starting batch clean for {len(tasks)} lines (parallel)")
        self._status(f"Cleaning {len(tasks)} lines in parallel")
        # If only a subset of lines is being cleaned, force untouched lines closed-to-chip
        # so they are isolated during the cleaning routine.
        try:
            selected_lines = {int(t.line) for t in tasks if t.line in (1, 2, 3)}
            untouched_lines = [ln for ln in (1, 2, 3) if ln not in selected_lines]
            if untouched_lines and getattr(self.robot_loader, "ser", None):
                for ln in untouched_lines:
                    print(f"[Servo] Close to chip: servo {ln + 3} (line {ln}) reason=other_line_cleaning")
                    self.robot_loader._set_servo_angle(self.robot_loader.ser, ln + 3, 40)
        except Exception as e:
            print(f"[RobotSequencer] Warning: could not isolate untouched lines during clean: {e}")
        for task in tasks:
            self._line_status(task.line, "Cleaning")
        with ThreadPoolExecutor(max_workers=min(3, len(tasks))) as executor:
            futures = {}
            for task in tasks:
                cleared_once = {"done": False}

                def _mark_line_empty_after_first_wash(line=task.line, flag=cleared_once):
                    if flag["done"]:
                        return
                    flag["done"] = True
                    self.lipid_tracker.clear_line(line)
                    self._line_status(line, "Empty/Clean (after wash 1)")
                    self._status(f"Line {line}: marked empty/clean after first wash")

                if task.clean_volume_ul is None:
                    fut = executor.submit(
                        self.robot_loader.full_clean_sequence,
                        task.line,
                        200,
                        bool(task.clean_through_chip),
                        _mark_line_empty_after_first_wash,
                    )
                else:
                    fut = executor.submit(
                        self.robot_loader.full_clean_sequence,
                        task.line,
                        float(task.clean_volume_ul),
                        bool(task.clean_through_chip),
                        _mark_line_empty_after_first_wash,
                    )
                futures[fut] = task.line
            for future, line in futures.items():
                try:
                    future.result()
                    self._status(f"Line {line}: Clean complete")
                    self._line_status(line, "Clean complete")
                except Exception as e:
                    print(f"[RobotSequencer] Clean error for line {line}: {e}")
                    self._status(f"Line {line}: ERROR during clean")
                    self._line_status(line, "Clean failed")
                    raise
    def _execute_pre_dobot_steps(self, task: RobotTask) -> None:
        """Execute prime ethanol and air flush for a single line (can be parallelized)."""
        line = task.line
        print(f"\n[Robot] >>> PRE-DOBOT SETUP: Line {line}")
        
        # Step 1: Prime ethanol
        print(f"[Robot] STEP 1/2: Priming ethanol to T-junction for line {line}")
        self._status(f"Line {line}: Prime ethanol to T-junction")
        self.robot_loader.prime_ethanol_to_tjunction(line)
        print(f"[Robot] ✓ STEP 1 COMPLETE: Ethanol prime done for line {line}")

        # Step 2: Air flush
        print(f"[Robot] STEP 2/2: Pre-load air flush for line {line}")
        self._status(f"Line {line}: Pre-load air flush")
        self.robot_loader.flush_main_line_with_air(
            line,
            flush_through_chip=bool(getattr(self.robot_loader, "load_flush_through_chip", False)),
        )
        print(f"[Robot] ✓ STEP 2 COMPLETE: Air flush done for line {line}")
        
        print(f"[Robot] <<< PRE-DOBOT SETUP COMPLETE: Line {line} ready for loading\n")

    def _execute_dobot_and_load(self, task: RobotTask) -> None:
        """Execute dobot pick/place and loading sequence (must be sequential per arm)."""
        if not self.plate_manager:
            raise RuntimeError("Plate calibration not set")
        
        if not self.robot_loader:
            raise RuntimeError("Robot loader not initialized - cannot execute load sequence")

        hold_plate, hold_row, hold_col = self.HOLDING_POSITIONS[task.line]

        # Optional stopper removal before moving nozzle to intake well.
        if bool(self.remove_stoppers_enabled):
            print(f"[Robot] Removing stopper at intake well ({task.plate},{task.row},{task.col})")
            self._status(f"Line {task.line}: Remove stopper at intake")
            self.remove_stopper_sequence(task.plate, task.row, task.col)

        # Step 1: Pick from holding position (plate 0)
        print(f"[Robot] Picking line {task.line} from holding ({hold_plate},{hold_row},{hold_col})")
        self._status(f"Line {task.line}: Pick from holding")
        self._pick_up(hold_plate, hold_row, hold_col)

        # Step 2: Place at intake well
        print(f"[Robot] Placing at intake well ({task.plate},{task.row},{task.col})")
        self._status(f"Line {task.line}: Place at intake well")
        loading_handoff = int(task.plate) in (1, 2, 3)
        self._place_down(task.plate, task.row, task.col, loading_handoff=loading_handoff)

        # Step 3: Execute loading sequence (fluid sensor polling)
        print(f"[Robot] Executing load sequence for line {task.line}")
        self._status(f"Line {task.line}: Loading sample")
        self.robot_loader.load_sample(task.line)
        print(f"[Robot] Load sequence complete for line {task.line}")
        
        # Step 4: Pick from intake well (only after load completes)
        print(f"[Robot] Picking from intake well")
        self._status(f"Line {task.line}: Pick from intake")
        self._pick_up(task.plate, task.row, task.col, loading_handoff=loading_handoff)

        # Step 5: Place back at holding position
        print(f"[Robot] Returning to holding position")
        self._status(f"Line {task.line}: Return to holding")
        self._place_down(hold_plate, hold_row, hold_col)

        # Assume 450uL loaded volume per line
        try:
            self.lipid_tracker.load_lipid_to_line(task.line, task.plate, task.row, task.col, task.lipid_name, 450)
            print(f"[Robot] Line {task.line} loaded with {task.lipid_name}")
        except Exception as e:
            # Ensure intake allocation is cleared even if state update fails
            try:
                self.lipid_tracker.remove_from_intake(task.plate, task.row, task.col)
                print(f"[Robot] Intake allocation cleared for P{task.plate} {task.row},{task.col} after load")
            except Exception:
                pass
            raise

    def _move_to_holding(self, line: int) -> None:
        """Move to holding position."""
        plate, row, col = self.HOLDING_POSITIONS[line]
        print(f"[Robot] Moving line {line} to holding position ({plate},{row},{col})")
        # dobotcontrol_multiplates.send_abs_move(...)

    def move_to_intake_hover(self, plate: int, row: int, col: int, hover_offset_mm: float = 50.0) -> None:
        """Move Dobot above an intake well without pick/place actions."""
        if not self.dobot_client:
            raise RuntimeError("Dobot is not connected.")
        if not self.plate_manager:
            raise RuntimeError("Plate calibration not set.")

        x, y, z, r = self.plate_manager.get_pose(int(plate), int(row), int(col), mode="enter")
        target_z = z + float(hover_offset_mm)
        print(f"[Robot] Hover move to intake well P{plate} {row},{col} at z+{hover_offset_mm:.1f} mm")
        if send_abs_move(self.dobot_client, x, y, target_z, r) is None:
            raise RuntimeError("Move to intake hover pose failed.")
        self._manual_pose = (x, y, target_z, r)

    def jog_manual(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0, dr: float = 0.0) -> None:
        """Jog Dobot from last tracked manual pose."""
        if not self.dobot_client:
            raise RuntimeError("Dobot is not connected.")
        if not self._manual_pose:
            raise RuntimeError("Manual pose is not initialized. Move to intake hover first.")
        x, y, z, r = self._manual_pose
        target = (x + float(dx), y + float(dy), z + float(dz), r + float(dr))
        if send_abs_move(self.dobot_client, target[0], target[1], target[2], target[3]) is None:
            raise RuntimeError("Manual jog move failed.")
        self._manual_pose = target

    def remove_stopper_sequence(
        self,
        plate: int,
        row: int,
        col: int,
        *,
        hover_offset_mm: float = 50.0,
        pickup_drop_mm: float = 42.0,
        dispose_forward_mm: float = 60.0,
    ) -> None:
        """Remove stopper from intake well and drop at disposal point."""
        if not self.dobot_client:
            raise RuntimeError("Dobot is not connected.")
        if not self.plate_manager:
            raise RuntimeError("Plate calibration not set.")

        plate = int(plate)
        row = int(row)
        col = int(col)

        src_x, src_y, src_z, src_r = self.plate_manager.get_pose(plate, row, col, mode="enter")
        src_hover_z = src_z + float(hover_offset_mm)
        pickup_z = src_hover_z - float(pickup_drop_mm)

        # 1) Hover above selected intake well.
        if send_abs_move(self.dobot_client, src_x, src_y, src_hover_z, src_r) is None:
            raise RuntimeError("Failed to move above selected intake well.")

        # 2) Open gripper, move down, close gripper, then return above.
        set_output(self.dobot_client, 2, "on")
        time.sleep(0.3)
        if send_abs_move(self.dobot_client, src_x, src_y, pickup_z, src_r) is None:
            raise RuntimeError("Failed to move down to stopper pickup depth.")
        set_output(self.dobot_client, 2, "off")
        time.sleep(0.3)
        if send_abs_move(self.dobot_client, src_x, src_y, src_hover_z, src_r) is None:
            raise RuntimeError("Failed to move back above selected intake well.")

        # 3) Move to disposal pose near P3 A3 (forward +Y offset), then open gripper.
        dst_x, dst_y, dst_z, dst_r = self.plate_manager.get_pose(3, 1, 3, mode="enter")
        dst_hover_z = dst_z + float(hover_offset_mm)
        if send_abs_move(
            self.dobot_client,
            dst_x,
            dst_y + float(dispose_forward_mm),
            dst_hover_z,
            dst_r,
        ) is None:
            raise RuntimeError("Failed to move to stopper disposal pose.")
        set_output(self.dobot_client, 2, "on")
        time.sleep(0.3)

        # 4) Return to configured home hover position (P2 R3 C2).
        home_x, home_y, home_z, home_r = self.plate_manager.get_pose(2, 3, 2, mode="enter")
        home_hover_z = home_z + float(hover_offset_mm)
        if send_abs_move(self.dobot_client, home_x, home_y, home_hover_z, home_r) is None:
            raise RuntimeError("Failed to return to home pose.")
        self._manual_pose = (home_x, home_y, home_hover_z, home_r)

    def _pick_up(self, plate: int, row: int, col: int, loading_handoff: bool = False) -> None:
        """Pick up vial from specified position."""
        # Intake loading handoff mode:
        # resume from lock pose after place_down and lift directly without toggling grip.
        if loading_handoff and int(plate) in (1, 2, 3):
            x, y, z, r = self.plate_manager.get_pose(plate, row, col, mode="lock")
            if send_abs_move(self.dobot_client, x, y, z + 1, r) is None:
                raise RuntimeError("Move to ENTER failed.")
            x, y, z, r = self.plate_manager.get_pose(plate, row, col, mode="enter")
            if send_abs_move(self.dobot_client, x, y, z, r + 5) is None:
                raise RuntimeError("Move to LOCK failed.")
            time.sleep(1)
            if send_abs_move(self.dobot_client, x, y, z + 50, r) is None:
                raise RuntimeError("Move to ENTER failed.")
            return

        # gripper open
        set_output(self.dobot_client, 2, "on")
        time.sleep(1)

        if plate == 0:
            x_add = 0
            y_add = 30
        else:
            x_add = 30
            y_add = 0

        print(f"[Robot] Pick offsets: x_add={x_add}, y_add={y_add}")

        x, y, z, r = self.plate_manager.get_pose(plate, row, col, mode="lock")

        if plate == 0:
            if send_abs_move(self.dobot_client, x + 30, y + y_add, z + 30, r) is None:
                raise RuntimeError("Move to ABOVE failed.")
       
        if send_abs_move(self.dobot_client, x + x_add, y + y_add, z + 30, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        if send_abs_move(self.dobot_client, x + x_add, y + y_add, z, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        if send_abs_move(self.dobot_client, x, y, z, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        if send_abs_move(self.dobot_client, x - (x_add / 30), y - (y_add / 30), z - 3.5, r) is None:
            raise RuntimeError("Move to ENTER failed.")

        # gripper closed
        set_output(self.dobot_client, 2, "off")
        time.sleep(4)  # Increased delay to ensure gripper fully closes and secures vial

        if send_abs_move(self.dobot_client, x, y, z + 1, r) is None:
            raise RuntimeError("Move to ENTER failed.")

        x, y, z, r = self.plate_manager.get_pose(plate, row, col, mode="enter")
        if send_abs_move(self.dobot_client, x, y, z, r + 5) is None:
            raise RuntimeError("Move to LOCK failed.")

        time.sleep(1)

        if send_abs_move(self.dobot_client, x, y, z + 50, r) is None:
            raise RuntimeError("Move to ENTER failed.")

    def _place_down(self, plate: int, row: int, col: int, loading_handoff: bool = False) -> None:
        """Place down vial at specified position."""
        if plate == 0:
            x_add = 0
            y_add = 30
        else:
            x_add = 30
            y_add = 0

        # gripper off
        set_output(self.dobot_client, 2, "off")
        time.sleep(1)

        x, y, z, r = self.plate_manager.get_pose(plate, row, col, mode="enter")
        if send_abs_move(self.dobot_client, x, y, z + 50, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        if send_abs_move(self.dobot_client, x, y, z + 20, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        if send_abs_move(self.dobot_client, x, y, z, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        time.sleep(0.5)

        if send_abs_move(self.dobot_client, x, y, z - 7, r) is None:
            raise RuntimeError("Move to ABOVE failed.")
        
        time.sleep(1)

        if send_abs_move(self.dobot_client, x, y, z, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        x, y, z, r = self.plate_manager.get_pose(plate, row, col, mode="lock")
        if send_abs_move(self.dobot_client, x, y, z, r) is None:
            raise RuntimeError("Move to LOCK failed.")

        # Intake loading handoff mode:
        # keep position at lock and keep current grip state so pick_up can resume directly.
        if loading_handoff and int(plate) in (1, 2, 3):
            return

        # vacuum/gripper off
        set_output(self.dobot_client, 2, "on")
        time.sleep(1)

        if send_abs_move(self.dobot_client, x + x_add, y + y_add, z, r) is None:
            raise RuntimeError("Move to ABOVE failed.")

        if send_abs_move(self.dobot_client, x + 30, y + 30, z + 60, r) is None:
            raise RuntimeError("Move to ABOVE failed.")
