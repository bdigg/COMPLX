from model import AppModel
from status_manager import StatusManager
from config_manager import ConfigManager
from queue_manager import QueueManager
from robot_sequencer import RobotSequencer
from plate_tracker import PlateTracker
from data_logger import DataLogger
from status_broker import StatusBroker
from microfluidic_controller import MicrofluidicController
import time
import random
from robot_loader import RobotClient, RobotLoader
import threading
import os
import json
import csv
import io
from lipid_tracker import LipidTracker
from typing import List, Tuple, Dict, Optional
import ast
import re
import itertools

class ControlAPI:
    def __init__(self):
        self.model = AppModel()
        self.status_mgr = StatusManager()
        self.config_mgr = ConfigManager()
        self.status_broker = StatusBroker()
        self.data_logger = DataLogger()
        self.lipid_manager = LipidTracker()
        self.plate_tracker = PlateTracker()
        self.queue_manager = QueueManager(self.config_mgr)
        self.robot_sequencer = RobotSequencer(self.lipid_manager)
        self.robot_sequencer.set_status_callback(self._on_robot_sequencer_status)
        self.robot_sequencer.set_error_callback(self._on_robot_fault)
        self.robot_sequencer.set_line_status_callback(self.status_broker.set_line_status)
        self.microfluidic_ctrl = MicrofluidicController(
            self.status_broker, 
            self.data_logger, 
            self.lipid_manager,
            on_composition_complete=self._on_composition_complete,
            on_experiment_complete=self._on_experiment_complete
        )
        self.gui = None
        self._last_used = self.config_mgr.load_config("app", "last_used") or {}
        self._queue_thread = None
        self._queue_stop = threading.Event()
        self._queue_running = False
        self._start_lock = threading.Lock()
        self._skip_loading = False
        self._start_well = (1, 1, 1)
        self._app_config = self.config_mgr.load_config("app", "config") or {}
        selected_cfg = self.config_mgr.load_config("app", "selected_config") or {}
        self._selected_config_preset = str(selected_cfg.get("name") or "").strip() or None
        self._plate_calibration = self.config_mgr.load_config("app", "plate_calibration") or {}
        self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)
        self.robot_sequencer.set_remove_stoppers_enabled(bool(self._app_config.get("Remove Stoppers", False)))
        self._buffer_selected_name = None
        self._clean_lock = threading.Lock()
        self._clean_state = {1: "Idle", 2: "Idle", 3: "Idle"}
        self._loaded_lines_this_exp = set()
        self._last_load_failure = None
        self._recovery_lock = threading.Lock()
        self._dobot_recovery_active = False
        self._dobot_recovery_context = None
        self._dobot_recovery_continue = threading.Event()
        self._active_log_exp_id = None
        self._admin_motion_lock = threading.Lock()
        self._admin_motion_thread = None
        self._random_dobot_lock = threading.Lock()
        self._random_dobot_thread = None
        self._random_dobot_stop = threading.Event()
        
        # Clear lipid allocations unless resuming from checkpoint
        if not self.has_checkpoint():
            self.clear_lipid_allocations()

    def set_gui(self, gui):
        self.gui = gui

    def toggle_connection(self, name):
        connected = self.model.toggle_connection(name)
        return connected

    def get_lipid_configs(self):
        return self.config_mgr.list_configs("lipids")

    def _enrich_lipid_entries_with_codes(self, lipid_entries: List[Dict]) -> List[Dict]:
        """Best-effort attach `lipid_code` from lipid library config to runtime lipid entries."""
        out: List[Dict] = []
        for entry in (lipid_entries or []):
            if not isinstance(entry, dict):
                out.append(entry)
                continue
            item = dict(entry)
            name = str(item.get("name") or "").strip()
            if name and not str(item.get("lipid_code") or "").strip():
                try:
                    cfg = self.load_lipid_config(name) or {}
                    code = str(cfg.get("lipid_code") or "").strip().upper()
                    if code:
                        item["lipid_code"] = code
                except Exception:
                    pass
            out.append(item)
        return out

    def save_lipid_config(self, name, data):
        code = str((data or {}).get("lipid_code", "")).strip().upper()
        if code:
            if not re.fullmatch(r"[A-Z0-9]+", code):
                raise ValueError("Lipid code must contain only A-Z and 0-9 (no spaces).")
            for other_name in self.get_lipid_configs():
                if other_name == name:
                    continue
                other_cfg = self.load_lipid_config(other_name) or {}
                other_code = str(other_cfg.get("lipid_code", "")).strip().upper()
                if other_code and other_code == code:
                    raise ValueError(f"Lipid code '{code}' is already used by '{other_name}'.")
            data = dict(data or {})
            data["lipid_code"] = code
        self.config_mgr.save_config("lipids", name, data)

    def load_lipid_config(self, name):
        return self.config_mgr.load_config("lipids", name)

    def get_microfluidics_configs(self):
        return self.config_mgr.list_configs("microfluidics")

    def save_microfluidics_config(self, name, data):
        self.config_mgr.save_config("microfluidics", name, data)

    def get_dobot_configs(self):
        return self.config_mgr.list_configs("dobot")

    def save_dobot_config(self, name, data):
        self.config_mgr.save_config("dobot", name, data)

    def get_buffer_configs(self):
        return self.config_mgr.list_configs("buffers")

    def save_buffer_config(self, name, data):
        self.config_mgr.save_config("buffers", name, data)

    def set_status(self, state, message=""):
        self.status_mgr.set_status(state, message)
        status_text = f"{state} - {message}" if message else state
        self.status_broker.set_ui_status(status_text)
        if self.gui:
            self.gui.status_label.setText(f"Status: {status_text}")

    def get_status(self):
        return self.status_broker.get_status()

    def get_experiment_record_number(self, exp_id: str) -> Optional[int]:
        """Best-effort lookup of the records folder numeric ID assigned at run start."""
        try:
            exp = self.queue_manager.get_experiment(str(exp_id))
            if exp and getattr(exp, "record_id", None) is not None:
                return int(exp.record_id)
        except Exception:
            pass
        try:
            return self.data_logger.get_record_number(str(exp_id))
        except Exception:
            return None

    def _compute_runtime_slot_permutation(self, lipid_stocks: List[Dict]) -> List[int]:
        """
        Choose a slot permutation (<=3 lipids) that maximizes reuse of currently-loaded lines.
        Returns permutation mapping new_slot_idx -> old_slot_idx.
        """
        n = len(lipid_stocks or [])
        if n <= 1:
            return list(range(n))
        target_names = [str((lipid_stocks[i] or {}).get("name", "")).strip() for i in range(n)]
        current_names = [
            str((self.lipid_manager.get_line_state(i + 1) or {}).get("lipid_name") or "").strip()
            for i in range(n)
        ]
        return self._compute_slot_permutation_from_current_names(target_names, current_names)

    def _compute_slot_permutation_from_current_names(self, target_names: List[str], current_names: List[str]) -> List[int]:
        """Pure helper: compute slot permutation from target and current slot names."""
        n = len(target_names or [])
        if n <= 1:
            return list(range(n))
        best_perm = list(range(n))
        best_score = None
        for perm_tuple in itertools.permutations(range(n)):
            perm = list(perm_tuple)
            score_matches = sum(
                1 for new_idx, old_idx in enumerate(perm)
                if current_names[new_idx] and target_names[old_idx] == current_names[new_idx]
            )
            # Tie-breakers: prefer identity/minimal movement.
            score_moved = sum(1 for new_idx, old_idx in enumerate(perm) if new_idx != old_idx)
            score = (score_matches, -score_moved)
            if best_score is None or score > best_score:
                best_score = score
                best_perm = perm
        return best_perm

    def _apply_runtime_slot_permutation(
        self,
        lipid_stocks: List[Dict],
        compositions: List[List[float]],
        flow_rates: List[List[float]],
        perm: List[int],
    ) -> Tuple[List[Dict], List[List[float]], List[List[float]]]:
        """Reorder lipid slots + composition columns + lipid flow-rate columns using new->old permutation."""
        if not perm or perm == list(range(len(perm))):
            return (
                [dict(x) if isinstance(x, dict) else x for x in (lipid_stocks or [])],
                [list(c) for c in (compositions or [])],
                [list(fr) for fr in (flow_rates or [])],
            )
        n = len(perm)
        new_lipids = [lipid_stocks[i] for i in perm]
        new_comps: List[List[float]] = []
        for comp in compositions or []:
            comp_list = list(comp)
            new_comps.append([comp_list[i] if i < len(comp_list) else 0.0 for i in perm])
        new_frs: List[List[float]] = []
        for fr in flow_rates or []:
            fr_list = list(fr)
            lipid_frs = list(fr_list[1:4])
            while len(lipid_frs) < 3:
                lipid_frs.append(0.0)
            reordered = [0.0, 0.0, 0.0]
            for new_idx, old_idx in enumerate(perm):
                if new_idx < 3 and old_idx < 3:
                    reordered[new_idx] = lipid_frs[old_idx]
            new_frs.append([fr_list[0]] + reordered + fr_list[4:])
        return new_lipids, new_comps, new_frs

    def _compute_runtime_physical_line_assignment(self, lipid_stocks: List[Dict]) -> List[int]:
        """
        Assign each lipid slot to a physical line (1..3), allowing sparse use (e.g. lines 1 & 3).
        Priority:
        1) reuse line that already has the same lipid
        2) use empty line
        3) replace line with different lipid
        """
        return self._compute_runtime_physical_line_assignment_with_pool(lipid_stocks, allowed_lines=None)

    def _get_active_line_pool(self, cfg: Optional[Dict] = None) -> List[int]:
        """Parse configured active lines and return ordered unique line ids in [1,2,3]."""
        cfg_map = cfg if isinstance(cfg, dict) else self._app_config
        raw = (cfg_map or {}).get("ActiveLines", [1, 2, 3])
        vals = raw
        if isinstance(raw, str):
            txt = raw.strip()
            try:
                vals = ast.literal_eval(txt)
            except Exception:
                vals = [x.strip() for x in txt.split(",") if x.strip()]
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        out: List[int] = []
        seen = set()
        for v in vals:
            try:
                iv = int(v)
            except Exception:
                continue
            if iv in (1, 2, 3) and iv not in seen:
                out.append(iv)
                seen.add(iv)
        return out if out else [1, 2, 3]

    def _default_runtime_line_assignment(self, n: int, allowed_lines: Optional[List[int]] = None) -> List[int]:
        pool = list(allowed_lines or [1, 2, 3])
        if n <= 0:
            return []
        if n > len(pool):
            raise ValueError(
                f"ActiveLines has {len(pool)} line(s), but experiment needs {n} lipid line(s)."
            )
        return pool[:n]

    def _compute_runtime_physical_line_assignment_with_pool(
        self,
        lipid_stocks: List[Dict],
        allowed_lines: Optional[List[int]] = None,
    ) -> List[int]:
        """
        Assign each lipid slot to a physical line from allowed_lines.
        Priority:
        1) reuse line that already has the same lipid
        2) use empty line
        3) replace line with different lipid
        """
        n = len(lipid_stocks or [])
        if n <= 0:
            return []
        candidate_lines = list(allowed_lines or [1, 2, 3])
        if n > len(candidate_lines):
            raise ValueError(
                f"ActiveLines has {len(candidate_lines)} line(s), but experiment needs {n} lipid line(s)."
            )

        target_names = [str((lipid_stocks[i] or {}).get("name", "")).strip() for i in range(n)]
        line_states = {line: (self.lipid_manager.get_line_state(line) or {}) for line in (1, 2, 3)}

        best_assignment: List[int] = list(candidate_lines[:n])
        best_score = None
        for perm in itertools.permutations(candidate_lines, n):
            exact = 0
            empty = 0
            replace = 0
            # soft preference to keep deterministic/compact ordering if no better signal
            line_order_penalty = 0
            for slot_idx, line in enumerate(perm):
                state = line_states.get(int(line), {})
                current_name = str(state.get("lipid_name") or "").strip()
                current_vol = float(state.get("remaining_volume", 0) or 0)
                target_name = target_names[slot_idx]
                line_order_penalty += abs((slot_idx + 1) - int(line))
                if current_name and current_vol > 0:
                    if current_name == target_name:
                        exact += 1
                    else:
                        replace += 1
                else:
                    empty += 1
            score = (
                exact,            # maximize reuse of same lipid
                empty,            # then maximize empty-line use
                -replace,         # minimize replacements
                -line_order_penalty,  # mild preference for compact/expected line numbers
            )
            if best_score is None or score > best_score:
                best_score = score
                best_assignment = list(perm)
        return best_assignment

    def _compute_physical_line_assignment_from_states(
        self,
        lipid_stocks: List[Dict],
        line_states: Dict[int, Dict],
        allowed_lines: Optional[List[int]] = None,
    ) -> List[int]:
        """Pure helper for estimating/assigning slot->line from provided line states."""
        n = len(lipid_stocks or [])
        if n <= 0:
            return []
        target_names = [str((lipid_stocks[i] or {}).get("name", "")).strip() for i in range(n)]
        candidate_lines = list(allowed_lines or [1, 2, 3])
        if n > len(candidate_lines):
            raise ValueError(
                f"ActiveLines has {len(candidate_lines)} line(s), but experiment needs {n} lipid line(s)."
            )
        best_assignment: List[int] = list(candidate_lines[:n])
        best_score = None
        for perm in itertools.permutations(candidate_lines, n):
            exact = empty = replace = 0
            line_order_penalty = 0
            for slot_idx, line in enumerate(perm):
                state = dict(line_states.get(int(line), {}) or {})
                current_name = str(state.get("lipid_name") or "").strip()
                current_vol = float(state.get("remaining_volume", 0) or 0)
                target_name = target_names[slot_idx]
                line_order_penalty += abs((slot_idx + 1) - int(line))
                if current_name and current_vol > 0:
                    if current_name == target_name:
                        exact += 1
                    else:
                        replace += 1
                else:
                    empty += 1
            score = (exact, empty, -replace, -line_order_penalty)
            if best_score is None or score > best_score:
                best_score = score
                best_assignment = list(perm)
        return best_assignment

    def _map_slot_flow_rates_to_physical_lines(
        self,
        flow_rates: List[List[float]],
        slot_to_line: List[int],
    ) -> List[List[float]]:
        """
        Convert logical slot-ordered full FR rows [buf, lip1, lip2, lip3]
        into physical-channel full FR rows where lipid slot i is written to channel (line+1).
        """
        out: List[List[float]] = []
        for fr in (flow_rates or []):
            src = list(fr)
            while len(src) < 4:
                src.append(0.0)
            dst = [float(src[0]), 0.0, 0.0, 0.0]
            for slot_idx, line in enumerate(slot_to_line):
                src_idx = slot_idx + 1
                dst_idx = int(line)  # line1->idx1(ch2), line2->idx2(ch3), line3->idx3(ch4)
                if 0 <= src_idx < len(src) and 0 <= dst_idx < len(dst):
                    dst[dst_idx] = float(src[src_idx])
            # Preserve any explicit channel-4 flow (e.g., line-3 constant-flow mode)
            # when physical line 3 is not used for a lipid slot assignment.
            if 3 not in [int(x) for x in (slot_to_line or [])]:
                dst[3] = float(src[3])
            out.append(dst)
        return out

    def estimate_queue_line_assignments(self) -> Dict[str, Dict]:
        """Estimate per-experiment runtime slot->line assignment from queue order."""
        out: Dict[str, Dict] = {}
        queue = self.queue_manager.get_queue()
        dyn = bool(self._app_config.get("dynamic_line_remap", False))
        allowed_lines = self._get_active_line_pool(self._app_config)
        line3_const_feature = bool(
            self._app_config.get("line3_RNA_constant", self._app_config.get("line3_constant_mode_enabled", False))
        )
        sim_states = {
            1: {"lipid_name": None, "remaining_volume": 0},
            2: {"lipid_name": None, "remaining_volume": 0},
            3: {"lipid_name": None, "remaining_volume": 0},
        }
        for exp in queue:
            lipids = [dict(x) if isinstance(x, dict) else x for x in (exp.lipid_stocks or [])]
            line3_const = bool(getattr(exp, "line3_constant_flow_enabled", False)) and line3_const_feature
            extra_rna_controller_connected = bool(
                line3_const and getattr(self.microfluidic_ctrl, "extra_pump_connected", False)
            )
            allowed_lines_exp = [
                ln for ln in allowed_lines
                if (ln != 3 or not line3_const or extra_rna_controller_connected)
            ]
            est_perm = list(range(len(lipids)))
            est_lipids = [dict(x) if isinstance(x, dict) else x for x in lipids]
            if dyn and len(est_lipids) > 1:
                try:
                    # Match runtime behavior: slot reorder first, then physical line assignment.
                    target_names = [str((x or {}).get("name", "")).strip() for x in est_lipids]
                    current_names = [
                        str((sim_states.get(i + 1, {}) or {}).get("lipid_name") or "").strip()
                        for i in range(len(est_lipids))
                    ]
                    est_perm = self._compute_slot_permutation_from_current_names(target_names, current_names)
                    est_lipids, _, _ = self._apply_runtime_slot_permutation(est_lipids, [], [], est_perm)
                except Exception:
                    est_perm = list(range(len(est_lipids)))
            if dyn and est_lipids:
                    slot_to_line = self._compute_physical_line_assignment_from_states(
                        est_lipids,
                        sim_states,
                        allowed_lines=allowed_lines_exp,
                    )
            else:
                slot_to_line = self._default_runtime_line_assignment(len(est_lipids), allowed_lines=allowed_lines_exp)
            out[str(exp.exp_id)] = {
                "slot_perm_new_to_old": list(est_perm),
                "slot_to_line": list(slot_to_line),
                "lines_used": sorted(slot_to_line),
            }
            # Advance simulated state as if experiment left its assigned lines loaded.
            # Keep untouched lines as-is (matches actual behavior between experiments).
            for i, lipid in enumerate(est_lipids):
                if i < len(slot_to_line):
                    sim_states[int(slot_to_line[i])] = {
                        "lipid_name": str((lipid or {}).get("name") or ""),
                        "remaining_volume": 1.0,
                    }
        return out

    def get_lipid_line_states(self):
        """Expose lipid line states (name + remaining volume) for GUI display."""
        return {i: self.lipid_manager.get_line_state(i) for i in (1, 2, 3)}

    def get_start_well(self) -> tuple:
        """Get the configured start well."""
        return self.status_broker.get_start_well()

    def is_microcontroller_connected(self) -> bool:
        """Check whether microcontroller serial connection is active."""
        return bool(self.microfluidic_ctrl and self.microfluidic_ctrl.ser)

    def clean_all(
        self,
        *,
        flush_volume: int = 200,
        flush_through_chip: bool = False,
        wash_cycles: int = 1,
        lines: Optional[List[int]] = None,
    ):
        """Move to waste mode, then clean selected lines simultaneously."""
        status = self.status_broker.get_status()
        if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
            return False, "Stop the current run before cleaning."
        selected_lines = sorted({int(x) for x in (lines or [1, 2, 3]) if int(x) in (1, 2, 3)})
        if not selected_lines:
            return False, "Select at least one line to clean."

        # Ensure loader is ready (requires dobot + microcontroller)
        self._ensure_robot_loader()
        if not (hasattr(self.robot_sequencer, "robot_loader") and self.robot_sequencer.robot_loader):
            self.set_status("Error", "Robot loader not ready. Connect Dobot + microcontroller first.")
            return False, "Robot loader not ready. Connect Dobot + microcontroller first."

        self._ensure_waste_mode()
        self._clean_params = {
            "flush_volume": float(flush_volume),
            "flush_through_chip": bool(flush_through_chip),
            "wash_cycles": max(1, int(wash_cycles)),
            "lines": list(selected_lines),
        }
        # Initialize all lines as "Starting" at once
        with self._clean_lock:
            for ln in (1, 2, 3):
                self._clean_state[ln] = "Starting" if ln in selected_lines else "Idle"
            status_text = " | ".join(
                f"L{ln}: {self._clean_state[ln]}" for ln in (1, 2, 3)
            )
        self.set_status("Cleaning", status_text)

        # Run clean sequence in background (non-blocking)
        threading.Thread(target=self._run_clean_all, daemon=True).start()
        return True, ""
    
    def _run_clean_all(self):
        """Background clean-all routine."""
        stop_event = threading.Event()
        monitor_thread = threading.Thread(
            target=self._monitor_clean_pressures,
            args=(stop_event,),
            daemon=True,
        )
        monitor_thread.start()

        selected_lines = list(self._clean_params.get("lines", [1, 2, 3]))
        extra_rna_flush_active = self._start_admin_extra_rna_flush()
        try:
            start_barrier = threading.Barrier(max(1, len(selected_lines)))
            threads = []
            for line in selected_lines:
                thread = threading.Thread(target=self._clean_line, args=(line, start_barrier), daemon=True)
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join()

            # Reset lipid record state after cleaning completes
            for line in selected_lines:
                self.lipid_manager.clear_line(int(line))
                self._set_clean_state(int(line), "Complete")
            self.set_status("Cleaned", "Selected lines cleaned successfully")
        finally:
            if extra_rna_flush_active:
                self._stop_admin_extra_rna_flush()
            stop_event.set()
            monitor_thread.join(timeout=2.0)

    def _clean_line(self, line: int, start_barrier: threading.Barrier):
        """Clean a single line with status updates."""
        try:
            # Wait for all lines to be ready, then start together
            start_barrier.wait()
            self._set_clean_state(line, "Starting")
            if hasattr(self.robot_sequencer, 'robot_loader') and self.robot_sequencer.robot_loader:
                flush_volume = self._clean_params.get("flush_volume", 200.0)
                flush_through_chip = self._clean_params.get("flush_through_chip", False)
                wash_cycles = max(1, int(self._clean_params.get("wash_cycles", 1)))
                self._set_clean_state(line, "Flush air")
                self.robot_sequencer.robot_loader.flush_main_line_with_air(
                    line,
                    flush_through_chip=flush_through_chip,
                    air_pressure_mbar=float(getattr(self.robot_sequencer.robot_loader, "cleaning_flush_pressure_mbar", 70.0)),
                )
                for cycle_idx in range(wash_cycles):
                    cyc = cycle_idx + 1
                    suffix = f" ({cyc}/{wash_cycles})" if wash_cycles > 1 else ""
                    self._set_clean_state(line, f"Wash tubing{suffix}")
                    self.robot_sequencer.robot_loader.wash_tubing(
                        line,
                        target_vol=flush_volume,
                        flush_through_chip=flush_through_chip,
                    )
                    self._set_clean_state(line, f"Final air flush{suffix}")
                    self.robot_sequencer.robot_loader.flush_main_line_with_air(
                        line,
                        flush_through_chip=flush_through_chip,
                        air_pressure_mbar=float(getattr(self.robot_sequencer.robot_loader, "cleaning_flush_pressure_mbar", 70.0)),
                    )
            
            self._set_clean_state(line, "Complete")
        except Exception as e:
            self._set_clean_state(line, f"Error: {str(e)}")

    def _monitor_clean_pressures(self, stop_event: threading.Event, interval_s: float = 2.0) -> None:
        """Periodically print applied pressures during clean-all."""
        try:
            import pump
        except Exception as e:
            print(f"[CleanAll] Pressure monitor disabled: {e}")
            return

        calibarr = getattr(self.microfluidic_ctrl, "calibarr", None)
        if not calibarr:
            print("[CleanAll] Pressure monitor disabled: calibarr not ready")
            return

        while not stop_event.is_set():
            readings = []
            loader = getattr(self.robot_sequencer, "robot_loader", None)
            mc_lock = getattr(loader, "_mc_io_lock", None)
            lock_cm = mc_lock if mc_lock is not None else self._clean_lock
            with lock_cm:
                for line in (1, 2, 3):
                    try:
                        # Cleaning "line" maps to microfluidic pressure channel (line + 1):
                        # L1->ch2, L2->ch3, L3->ch4. Channel 1 is the buffer channel.
                        pressure_channel = int(line) + 1
                        p_val, p_err = pump.get_pressure_data(pressure_channel, calibarr)
                        if p_err:
                            readings.append(f"L{line}(ch{pressure_channel})=ERR({p_err})")
                        else:
                            readings.append(f"L{line}(ch{pressure_channel})={p_val:.1f} mbar")
                    except Exception as e:
                        readings.append(f"L{line}(ch{int(line)+1})=ERR({e})")
                try:
                    if self._admin_extra_rna_flush_should_run():
                        last_p = float(getattr(self.microfluidic_ctrl, "extra_pressure_last", 0.0) or 0.0)
                        readings.append(f"RNA(extra)={last_p:.1f} mbar")
                except Exception as e:
                    readings.append(f"RNA(extra)=ERR({e})")

            ts = time.strftime("%H:%M:%S")
            print(f"[CleanAll {ts}] Applied pressures: " + ", ".join(readings))
            stop_event.wait(interval_s)

    def _set_clean_state(self, line: int, state: str) -> None:
        """Aggregate clean status for all lines into one UI update."""
        with self._clean_lock:
            self._clean_state[line] = state
            status_text = " | ".join(
                f"L{ln}: {self._clean_state[ln]}" for ln in (1, 2, 3)
            )
        self.set_status("Cleaning", status_text)

    def _ensure_waste_mode(self):
        """Ensure waste mode before cleaning."""
        if hasattr(self.microfluidic_ctrl, "move_z_safe"):
            self.microfluidic_ctrl.move_z_safe()
        if hasattr(self.microfluidic_ctrl, "set_waste_mode"):
            self.microfluidic_ctrl.set_waste_mode()
        elif hasattr(self.microfluidic_ctrl, "set_valve_waste"):
            self.microfluidic_ctrl.set_valve_waste()

    def _admin_extra_rna_flush_should_run(self) -> bool:
        cfg = self.get_config() or {}
        rna_mode_on = bool(cfg.get("line3_RNA_constant", cfg.get("line3_constant_mode_enabled", False)))
        return bool(rna_mode_on and getattr(self.microfluidic_ctrl, "extra_pump_connected", False))

    def _start_admin_extra_rna_flush(self) -> bool:
        """Drive the extra RNA pressure controller during admin flush/clean runs."""
        if not self._admin_extra_rna_flush_should_run():
            return False
        pressure = float(self._app_config.get("cleaning_flush_pressure_mbar", 70.0) or 70.0)
        try:
            pmin = float(getattr(self.microfluidic_ctrl.extra_pump, "pressure_min", 0.0) or 0.0)
            pmax = float(getattr(self.microfluidic_ctrl.extra_pump, "pressure_max", 2000.0) or 2000.0)
            pressure = max(pmin, min(pmax, pressure))
            ok, err = self.microfluidic_ctrl.extra_pump.set_pressure(pressure)
            if not ok:
                print(f"[CleanAll] Extra RNA flush not started: {err}")
                return False
            self.microfluidic_ctrl.extra_pressure_last = float(pressure)
            print(f"[CleanAll] Extra RNA flush active at {pressure:.1f} mbar")
            return True
        except Exception as e:
            print(f"[CleanAll] Extra RNA flush not started: {e}")
            return False

    def _stop_admin_extra_rna_flush(self) -> None:
        try:
            ok, err = self.microfluidic_ctrl.extra_pump.set_pressure(0.0)
            if not ok:
                print(f"[CleanAll] Extra RNA flush stop warning: {err}")
            self.microfluidic_ctrl.extra_pressure_last = 0.0
            print("[CleanAll] Extra RNA flush stopped")
        except Exception as e:
            print(f"[CleanAll] Extra RNA flush stop warning: {e}")

    def has_checkpoint(self) -> bool:
        """Check for temp state from previous run."""
        path = "./temp_state/queue.json"
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return bool(data)
        except Exception:
            return False

    def resume_from_checkpoint(self):
        """Resume from existing temp state (queue already loaded by QueueManager)."""
        self.status_broker.set_microfluidic_state("Idle")
        # Notify GUI to refresh queue table and restore intake visualization
        if self.gui:
            self.gui._refresh_queue_table()
            self.gui._restore_intake_visualization()

    def clear_checkpoint(self):
        """Clear temp state and start fresh."""
        self.data_logger.cleanup_temp()
        self.queue_manager.queue = []
        self.queue_manager._save_queue_to_disk()
        self.clear_lipid_allocations()

    def clear_lipid_allocations(self):
        """Clear intake allocations on fresh start."""
        self.lipid_manager.intake_allocations.clear()
        self.lipid_manager._save_state()

    def delete_experiment(self, exp_id: str) -> None:
        """Delete an experiment from the queue."""
        self.queue_manager.delete_experiment(exp_id)
        print(f"[ControlAPI] Deleted experiment: {exp_id}")

    def repeat_experiment(self, exp_id: str) -> None:
        """Duplicate an experiment and append to queue."""
        new_exp = self.queue_manager.duplicate_experiment(exp_id)
        if new_exp:
            self._update_plate_map_for_experiment(new_exp)
            print(f"[ControlAPI] Duplicated experiment: {exp_id} -> {new_exp.exp_id}")

    def edit_experiment_in_queue(self, exp_id: str, exp_data: Dict) -> Tuple[bool, str]:
        """Edit an experiment (only if pending)."""
        try:
            exp_data = dict(exp_data or {})
            line3_enabled = bool(exp_data.get("line3_constant_flow_enabled", False))
            exp_data["line3_uses_main_pump"] = bool(line3_enabled and not self.is_extra_pressure_connected())
            exp = self.queue_manager.edit_experiment(exp_id, exp_data)
            if not exp:
                return False, "Experiment not found"
            return True, ""
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Edit failed: {str(e)}"

    def get_lipid_allocations(self):
        """Expose intake allocations for GUI restore."""
        return dict(self.lipid_manager.intake_allocations)

    def get_experiment_plot_colors(self, exp_id: str) -> List[str]:
        """Return plot colors for buffer + lipids for a given experiment."""
        queue = self.queue_manager.get_queue()
        exp = next((e for e in queue if e.exp_id == exp_id), None)
        if not exp:
            return ["#00FFFF", "#FFA500", "#FF0000", "#8A2BE2"]

        # Buffer color (fixed for now)
        colors = ["#00FFFF"]

        # Lipid colors from config
        for lipid in exp.lipid_stocks:
            cfg = self.config_mgr.load_config("lipid_colors", lipid["name"]) or {}
            colors.append(cfg.get("color", "#777777"))

        # Ensure length 4 (buffer + up to 3 lipids)
        while len(colors) < 4:
            colors.append("#777777")
        return colors[:4]

    def reassign_output_wells(self, start_well):
        """Reassign output wells from a new starting well."""
        self.queue_manager.reassign_output_wells(start_well)
        for exp in self.queue_manager.get_queue():
            self._update_plate_map_for_experiment(exp)

    def can_start(self):
        status = self.status_broker.get_status()
        conns = status.get("connections", {})
        all_connected = all(conns.get(k, False) for k in ("microfluidics", "dobot", "microcontroller"))
        if not all_connected:
            return False, "All connections must be successful before starting."
        if not self._selected_config_preset:
            return False, "Select a named config before starting."
        if self._selected_config_preset not in self.list_config_presets():
            return False, f"Selected config '{self._selected_config_preset}' was not found. Select another config."
        if not self._buffer_selected_name:
            return False, "Buffer must be selected before starting."
        return True, ""

    def start(self):
        """Start executing queued experiments sequentially."""
        return self._start_internal(skip_loading=False)

    def start_without_loading(self):
        """Start executing queued experiments without loading lipids (admin function)."""
        return self._start_internal(skip_loading=True)

    def _start_internal(self, skip_loading=False):
        """Internal start method with optional loading skip."""
        ok, err = self.can_start()
        if not ok:
            return False, err
        with self._start_lock:
            if self._queue_running or (self._queue_thread and self._queue_thread.is_alive()):
                return True, ""
            # Mark running immediately to prevent duplicate starts from rapid clicks/re-entry.
            self._queue_running = True
            self._queue_stop.clear()
            self._skip_loading = skip_loading
        try:
            self.set_status("Starting", "Initializing run")
            queue_snapshot = []
            try:
                queue_snapshot = [
                    {"exp_id": e.exp_id, "name": e.name, "status": e.status}
                    for e in self.queue_manager.get_queue()
                ]
            except Exception:
                queue_snapshot = []
            self.data_logger.begin_run(queue_snapshot=queue_snapshot)
            # Apply config before run
            self.microfluidic_ctrl.apply_config(self.get_config())
            self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)
            self._set_all_dobot_valves_off()
            if skip_loading:
                self._prime_skip_loading_line_states()
            self.microfluidic_ctrl.home_to_start(self._start_well)
            self.set_status("Starting", "Homed to start well")
            self.microfluidic_ctrl.start()
            self._queue_thread = threading.Thread(target=self._run_queue, daemon=True)
            self._queue_thread.start()
            return True, ""
        except Exception as e:
            self._queue_running = False
            self._queue_stop.set()
            self.data_logger.end_run()
            self.microfluidic_ctrl._enter_fail_safe(f"Start failed: {e}")
            return False, str(e)

    def _set_all_dobot_valves_off(self):
        """Ensure all dobot valves are off before microfluidic loop starts."""
        try:
            loader = getattr(self.robot_sequencer, "robot_loader", None)
            if not loader:
                return
            for line in (1, 2, 3):
                loader._set_dobot_valve(line, "off")
        except Exception as e:
            self.status_broker.set_error(f"Failed to set dobot valves off: {e}")

    def _prime_skip_loading_line_states(self):
        """On 'start without loading', assume all lipids loaded to 450uL immediately."""
        try:
            queue = self.queue_manager.get_queue()
            next_exp = next((e for e in queue if e.status in ("pending", "stopped", "paused")), None)
            if not next_exp:
                return
            for i, lipid in enumerate(next_exp.lipid_stocks):
                line_idx = i + 1
                with self.lipid_manager._lock:
                    self.lipid_manager.line_state[line_idx] = {
                        "lipid_name": lipid["name"],
                        "remaining_volume": 450,
                        "loaded_volume": 450,
                        "source_well": None
                    }
                self.lipid_manager._save_state()
        except Exception:
            pass

    def stop(self):
        """Stop current experiment and halt queue."""
        self._queue_stop.set()
        status = self.status_broker.get_status()
        exp_id = status.get("current_experiment")
        if exp_id:
            self.queue_manager.update_status(exp_id, "stopped", error="Stopped by user")
        self.microfluidic_ctrl.stop_experiment()
        self._queue_running = False
        self._active_log_exp_id = None
        self.data_logger.end_run()

    def shutdown(self):
        """Graceful shutdown: stop pressures and background loops."""
        try:
            self.microfluidic_ctrl.shutdown()
        except Exception:
            pass
        try:
            self.data_logger.shutdown()
        except Exception:
            pass

    def pause(self):
        self.microfluidic_ctrl.pause()
        self.status_broker.set_paused(True)

    def resume(self):
        self.microfluidic_ctrl.resume()
        self.status_broker.set_paused(False)

    def _on_robot_fault(self, fault: dict) -> None:
        if not fault or fault.get("code") != "DOBOT_UNRESPONSIVE":
            return
        with self._recovery_lock:
            if self._dobot_recovery_active:
                return
            self._dobot_recovery_active = True
            self._dobot_recovery_context = dict(fault)
            self._dobot_recovery_continue.clear()

        line = fault.get("line")
        lipid = fault.get("lipid_name")
        detail = f"Dobot unresponsive during line {line} load"
        if lipid:
            detail += f" ({lipid})"
        try:
            self.pause()
        except Exception:
            self.status_broker.set_paused(True)
        self.set_status("Paused", detail)
        self.status_broker.set_error(detail)
        self.status_broker.set_recovery_prompt({
            "type": "dobot_reconnect",
            "line": line,
            "lipid_name": lipid,
            "message": (
                "Dobot connection was lost or unresponsive.\n"
                "Reconnect Dobot, put nozzle back in holding position,\n"
                "then press Continue to resume."
            ),
        })
        print(f"[ControlAPI] Recovery required: {detail}")

    def continue_after_dobot_recovery(self) -> Tuple[bool, str]:
        with self._recovery_lock:
            if not self._dobot_recovery_active:
                return False, "No pending Dobot recovery."
        if not self._verify_dobot_connection():
            return False, "Dobot is not responding. Reconnect Dobot first."

        self.status_broker.set_error(None)
        self.status_broker.set_recovery_prompt(None)
        self.status_broker.set_paused(False)
        try:
            self.resume()
        except Exception:
            pass
        self._dobot_recovery_continue.set()
        self.set_status("Resuming", "Resuming after Dobot reconnect")
        return True, ""

    def _verify_dobot_connection(self) -> bool:
        client = getattr(self.robot_sequencer, "dobot_client", None)
        if not client:
            self.status_broker.set_connection_state("dobot", False)
            return False
        try:
            resp = client.request("di 1", safe_to_retry=False, timeout_s=0.75)
            ok = bool(resp)
            self.status_broker.set_connection_state("dobot", ok)
            return ok
        except Exception:
            self.status_broker.set_connection_state("dobot", False)
            return False

    def _wait_for_recovery_continue(self) -> bool:
        while not self._queue_stop.is_set():
            if self._dobot_recovery_continue.wait(timeout=0.2):
                self._dobot_recovery_continue.clear()
                with self._recovery_lock:
                    self._dobot_recovery_active = False
                    self._dobot_recovery_context = None
                return True
        return False

    def skip(self):
        self.microfluidic_ctrl.skip_composition()

    def _run_queue(self):
        """Sequentially run experiments in queue."""
        while not self._queue_stop.is_set():
            # Safety gate: never advance queue while robot still has pending work.
            # This prevents overlap/races where a new experiment can start while
            # line cleaning/loading tasks are still running.
            try:
                if self.robot_sequencer and self.robot_sequencer.is_busy():
                    self.set_status("Preparing", "Waiting for robot to become idle")
                    time.sleep(0.5)
                    continue
            except Exception:
                pass

            queue = self.queue_manager.get_queue()
            
            # Debug: Print all experiment statuses
            print(f"[ControlAPI] Checking queue for next experiment ({len(queue)} total):")
            for exp in queue:
                print(f"  - {exp.exp_id}: status={exp.status}, name={exp.name}")

            # Find next pending/stopped experiment
            next_exp = None
            for exp in queue:
                if exp.status in ("pending", "stopped", "paused"):
                    next_exp = exp
                    break

            if not next_exp:
                print("[ControlAPI] No pending experiments found - queue finished")
                self._active_log_exp_id = None
                # Stop all flows and move to waste
                self.microfluidic_ctrl.stop_all_pressures()
                try:
                    self.microfluidic_ctrl._end_collection_to_waste()
                except Exception as e:
                    print(f"[ControlAPI] Error ending collection to waste: {e}")
                self.set_status("Idle", "Queue finished - all pressures stopped")
                self._queue_running = False
                self.data_logger.end_run()
                return
            
            print(f"[ControlAPI] Starting next experiment: {next_exp.exp_id} ({next_exp.name})")
            self._active_log_exp_id = str(next_exp.exp_id)
            self._log_runtime_event("control", f"Starting experiment {next_exp.exp_id} ({next_exp.name})")

            # Optional runtime slot remapping: reorder lipid slots to better match currently-loaded lines.
            # This preserves the physical line assumptions (slot 1->line1, slot 2->line2, slot 3->line3)
            # while reducing unnecessary switching between experiments.
            cfg = self.get_config()
            enable_dynamic_line_remap = bool(cfg.get("dynamic_line_remap", False))
            allowed_lines = self._get_active_line_pool(cfg)
            line3_const_feature = bool(cfg.get("line3_RNA_constant", cfg.get("line3_constant_mode_enabled", False)))
            line3_constant_on = bool(getattr(next_exp, "line3_constant_flow_enabled", False)) and line3_const_feature
            line3_constant_rate = float(getattr(next_exp, "line3_constant_flow_rate", 0.0) or 0.0)
            if line3_constant_rate <= 0:
                line3_constant_on = False
            extra_rna_controller_connected = bool(
                line3_constant_on and getattr(self.microfluidic_ctrl, "extra_pump_connected", False)
            )
            runtime_allowed_lines = [
                ln for ln in allowed_lines
                if (ln != 3 or not line3_constant_on or extra_rna_controller_connected)
            ]
            runtime_perm = list(range(len(next_exp.lipid_stocks or [])))
            original_lipid_stocks_with_codes = self._enrich_lipid_entries_with_codes(
                [dict(x) if isinstance(x, dict) else x for x in (next_exp.lipid_stocks or [])]
            )
            runtime_lipid_stocks = [dict(x) if isinstance(x, dict) else x for x in original_lipid_stocks_with_codes]
            runtime_compositions = [list(c) for c in (next_exp.compositions or [])]
            runtime_flow_rates = [list(fr) for fr in (next_exp.flow_rates or [])]
            runtime_exec_flow_rates = [list(fr) for fr in runtime_flow_rates]
            try:
                runtime_line_assignment = self._default_runtime_line_assignment(
                    len(runtime_lipid_stocks),
                    allowed_lines=runtime_allowed_lines,
                )
            except Exception as e:
                reason = str(e)
                self.queue_manager.update_status(next_exp.exp_id, "error", error=reason)
                self.set_status("Error", f"{next_exp.name}: {reason}")
                continue
            if enable_dynamic_line_remap and len(runtime_lipid_stocks) > 1:
                try:
                    runtime_perm = self._compute_runtime_slot_permutation(runtime_lipid_stocks)
                    runtime_lipid_stocks, runtime_compositions, runtime_flow_rates = self._apply_runtime_slot_permutation(
                        runtime_lipid_stocks, runtime_compositions, runtime_flow_rates, runtime_perm
                    )
                    if runtime_perm != list(range(len(runtime_perm))):
                        orig_names = [str((x or {}).get("name", "")) for x in (next_exp.lipid_stocks or [])]
                        remap_names = [str((x or {}).get("name", "")) for x in (runtime_lipid_stocks or [])]
                        print(f"[ControlAPI] Dynamic line remap applied: perm(new->old)={runtime_perm} | {orig_names} -> {remap_names}")
                        self._log_runtime_event(
                            "control",
                            "Dynamic line remap applied",
                            details={
                                "perm_new_slot_to_old_slot": list(runtime_perm),
                                "original_lipid_order": orig_names,
                                "runtime_lipid_order": remap_names,
                            },
                        )
                except Exception as e:
                    print(f"[ControlAPI] Dynamic line remap skipped due to error: {e}")
            if enable_dynamic_line_remap and runtime_lipid_stocks:
                try:
                    runtime_line_assignment = self._compute_runtime_physical_line_assignment_with_pool(
                        runtime_lipid_stocks,
                        allowed_lines=runtime_allowed_lines,
                    )
                    if runtime_line_assignment != self._default_runtime_line_assignment(
                        len(runtime_lipid_stocks),
                        allowed_lines=runtime_allowed_lines,
                    ):
                        names = [str((x or {}).get("name", "")) for x in runtime_lipid_stocks]
                        print(
                            f"[ControlAPI] Dynamic physical line assignment applied: "
                            + ", ".join(f"{names[i]}->L{runtime_line_assignment[i]}" for i in range(len(names)))
                        )
                        self._log_runtime_event(
                            "control",
                            "Dynamic physical line assignment applied",
                            details={
                                "runtime_lipid_order": names,
                                "slot_to_line": list(runtime_line_assignment),
                            },
                        )
                except Exception as e:
                    print(f"[ControlAPI] Dynamic physical line assignment skipped due to error: {e}")
            try:
                runtime_exec_flow_rates = self._map_slot_flow_rates_to_physical_lines(
                    runtime_flow_rates, runtime_line_assignment
                )
            except Exception as e:
                print(f"[ControlAPI] Flow-rate physical mapping skipped due to error: {e}")
                runtime_exec_flow_rates = [list(fr) for fr in runtime_flow_rates]

            # Ensure lipids loaded before running (unless skip_loading is enabled)
            lipid_load_failed = False
            self._loaded_lines_this_exp = set()
            if self._skip_loading:
                self.set_status("Starting", f"{next_exp.name}: Skipping lipid loading (admin mode)")
                # Mark lipids as loaded without actually loading
                loader = getattr(self.robot_sequencer, "robot_loader", None)
                for i, lipid in enumerate(runtime_lipid_stocks):
                    line_idx = int(runtime_line_assignment[i]) if i < len(runtime_line_assignment) else (i + 1)
                    # Directly set line state without requiring intake allocation
                    with self.lipid_manager._lock:
                        self.lipid_manager.line_state[line_idx] = {
                            "lipid_name": lipid["name"],
                            "remaining_volume": 450,
                            "loaded_volume": 450,
                            "source_well": None
                        }
                
                # Set servo valves to proper post-load position
                if loader:
                    import expel
                    active_lines = sorted({int(x) for x in runtime_line_assignment[:len(runtime_lipid_stocks)]})
                    unused_lines = [l for l in (1, 2, 3) if l not in active_lines]
                    if line3_constant_on and not extra_rna_controller_connected:
                        unused_lines = [l for l in unused_lines if l != 3]
                    for line in active_lines:
                        expel.set_servo_angle(loader.ser, line, 40)  # Close to dobot
                        time.sleep(0.1)
                        expel.set_servo_angle(loader.ser, line + 3, 125)  # Close to waste
                        time.sleep(0.1)
                    for line in unused_lines:
                        expel.set_servo_angle(loader.ser, line, 40)  # Close to dobot
                        time.sleep(0.1)
                        print(f"[Servo] Close to chip: servo {line + 3} (line {line}) reason=skip_load_unused_line")
                        expel.set_servo_angle(loader.ser, line + 3, 40)  # Close to chip
                        time.sleep(0.1)
                    print("[ControlAPI] Skip-load: Active lines to waste, unused lines closed to chip")
                    self.set_status("Preparing", "Servo valves configured for pressure-driven mode")
                
                self.lipid_manager._save_state()
                # NOTE: Do NOT reset _skip_loading flag here - keep it for subsequent experiments in queue
            else:
                self._last_load_failure = None
                self.status_broker.set_all_line_status("Idle")
                lines_to_clean = []
                for i, lipid in enumerate(runtime_lipid_stocks):
                    line_idx = int(runtime_line_assignment[i]) if i < len(runtime_line_assignment) else (i + 1)
                    line_state = self.lipid_manager.get_line_state(line_idx)
                    current_name = line_state.get("lipid_name")
                    if current_name and current_name != lipid["name"]:
                        lines_to_clean.append(line_idx)

                if lines_to_clean:
                    self.set_status(
                        "Cleaning",
                        "Switching lines in parallel: " + ", ".join(f"L{ln}" for ln in lines_to_clean),
                    )
                    for line_idx in lines_to_clean:
                        self.status_broker.set_line_status(line_idx, "Cleaning")
                    self.robot_sequencer.queue_clean_lines(lines_to_clean, clean_through_chip=True)
                    if not self.robot_sequencer.wait_until_idle(timeout_s=900.0):
                        self.queue_manager.update_status(
                            next_exp.exp_id,
                            "error",
                            error="Timeout while cleaning switched lines",
                        )
                        self.set_status("Error", f"{next_exp.name}: Line switch cleaning timeout")
                        continue
                    clean_err = self.robot_sequencer.pop_last_error()
                    if clean_err:
                        self.queue_manager.update_status(
                            next_exp.exp_id,
                            "error",
                            error=f"Line switch cleaning failed: {clean_err}",
                        )
                        self.set_status("Error", f"{next_exp.name}: line switch cleaning failed")
                        self._queue_running = False
                        self._active_log_exp_id = None
                        self.data_logger.end_run()
                        return

                for i, lipid in enumerate(runtime_lipid_stocks):
                    line_idx = int(runtime_line_assignment[i]) if i < len(runtime_line_assignment) else (i + 1)
                    if not self._ensure_lipid_loaded(
                        line_idx,
                        lipid["name"],
                        assume_cleaned=(line_idx in lines_to_clean),
                    ):
                        fail = dict(self._last_load_failure or {})
                        if fail.get("type") == "missing_intake":
                            self.queue_manager.mark_experiments_red(lipid["name"])
                        lipid_load_failed = True
                
                if lipid_load_failed:
                    fail = dict(self._last_load_failure or {})
                    reason = fail.get("message") or "Failed to load required lipids"
                    self.queue_manager.update_status(next_exp.exp_id, "error", error=reason)
                    self.set_status("Error", f"{next_exp.name}: {reason}")
                    # Stop queue loop on load failure so we do not execute queue-finished end routine.
                    self._queue_running = False
                    self._active_log_exp_id = None
                    self.data_logger.end_run()
                    return
                
                # Wait for robot to return samples to holding positions
                self.set_status("Preparing", "Waiting for robot to return to ready state")
                if not self.robot_sequencer.wait_until_idle(timeout_s=300.0):
                    self.queue_manager.update_status(next_exp.exp_id, "error", error="Robot did not return to idle state in time")
                    self.set_status("Error", f"{next_exp.name}: Robot timeout waiting for idle")
                    continue
                
                # Critical: Ensure all dobot valves are off before starting experiment
                self._set_all_dobot_valves_off()
                self.set_status("Preparing", "All dobot valves confirmed off, ready to start")
            
            # Determine starting composition index (resume support)
            start_comp_idx = 0
            if next_exp.comp_status:
                for i, cs in enumerate(next_exp.comp_status):
                    if cs != "completed":
                        start_comp_idx = i
                        break
                else:
                    # All compositions already completed
                    self.queue_manager.update_status(next_exp.exp_id, "completed")
                    continue
            
            # Priming target selection:
            # - default: only lines actually loaded for this experiment
            # - config prime_all=True: when a post-load prime is required, expand it to all
            #   active runtime lines used by this experiment
            print(f"[Controller] DEBUG: start_comp_idx={start_comp_idx}, _skip_loading={self._skip_loading}")
            loaded_lines = sorted(self._loaded_lines_this_exp)
            active_runtime_lines = sorted({int(x) for x in runtime_line_assignment[:len(runtime_lipid_stocks)]})
            if line3_constant_on and not extra_rna_controller_connected:
                loaded_lines = [ln for ln in loaded_lines if ln != 3]
                active_runtime_lines = [ln for ln in active_runtime_lines if ln != 3]
            has_newly_loaded_lines = bool(loaded_lines)
            prime_all = bool(self._app_config.get("prime_all", False))
            priming_lines = list(loaded_lines)
            if prime_all and has_newly_loaded_lines:
                priming_lines = active_runtime_lines
            if (
                has_newly_loaded_lines
                and line3_constant_on
                and not extra_rna_controller_connected
                and line3_constant_rate > 0
                and 3 not in priming_lines
            ):
                priming_lines = sorted(set(priming_lines + [3]))
            if start_comp_idx == 0 and not self._skip_loading and has_newly_loaded_lines and priming_lines:
                print("[Controller] DEBUG: Priming condition TRUE - proceeding with priming")
                # Safety: ensure dobot is idle and all dobot valves are OFF before priming
                if self.robot_sequencer and not self.robot_sequencer.wait_until_idle(timeout_s=120.0):
                    self.queue_manager.update_status(next_exp.exp_id, "error", error="Robot not idle before priming")
                    self.set_status("Error", f"{next_exp.name}: Robot not idle before priming")
                    continue
                self._set_all_dobot_valves_off()
                time.sleep(10)

                priming_mode = "all active lines" if (prime_all and loaded_lines) else "newly loaded lines"
                print(
                    f"[Controller] DEBUG: Calling prime_lines with lines: {priming_lines} "
                    f"(mode={priming_mode})"
                )
                self.set_status("Priming", f"Priming {len(priming_lines)} lines...")
                # CRITICAL: Set state to something other than Idle/Ready BEFORE calling prime_lines
                # so the wait loop below will actually wait for priming to complete
                self.status_broker.set_microfluidic_state("Priming")
                self.microfluidic_ctrl.prime_lines(
                    priming_lines,
                    line3_constant_active=bool(
                        line3_constant_on
                        and not extra_rna_controller_connected
                        and line3_constant_rate > 0
                    ),
                    extra_rna_active=bool(
                        line3_constant_on
                        and extra_rna_controller_connected
                        and line3_constant_rate > 0
                    ),
                )
                print(f"[Controller] DEBUG: prime_lines() called, waiting for microfluidic_state to become Idle or Ready")
                # Wait for priming to complete (will transition to Idle when done)
                print(f"[Controller] DEBUG: Current state before wait loop: {self.status_broker.get_status()['microfluidic_state']}")
                max_wait_s = 900  # 15 minutes timeout
                elapsed_s = 0
                while self.status_broker.get_status()["microfluidic_state"] not in ("Idle", "Ready"):
                    if elapsed_s > max_wait_s:
                        print(f"[Controller] ERROR: Priming timeout after {max_wait_s}s, state={self.status_broker.get_status()['microfluidic_state']}")
                        self.queue_manager.update_status(next_exp.exp_id, "error", error=f"Priming timeout")
                        break
                    time.sleep(0.5)
                    elapsed_s += 0.5
                print(f"[Controller] DEBUG: Priming complete! Final state: {self.status_broker.get_status()['microfluidic_state']} (waited {elapsed_s:.1f}s)")
                self.set_status("Preparing", "Priming complete, starting experiment")
            elif start_comp_idx == 0 and not self._skip_loading and not has_newly_loaded_lines:
                print("[Controller] DEBUG: Priming skipped - no newly loaded lines in this run")

            # Mark running
            self.queue_manager.update_status(next_exp.exp_id, "running")
            self.set_status("Running", f"Experiment {next_exp.name} starting")

            # Pull stable-flow expulsion hold time from config (seconds)
            expul_t = cfg.get("expul_t", 13.4)
            try:
                expul_t = float(expul_t)
            except Exception:
                expul_t = 13.4
            if expul_t < 0:
                expul_t = 0.0
            maxfrerror = cfg.get("maxfrerror", [100, 0.2])
            if isinstance(maxfrerror, str):
                try:
                    maxfrerror = ast.literal_eval(maxfrerror)
                except Exception:
                    maxfrerror = [100, 0.2]
            if not isinstance(maxfrerror, (list, tuple)) or len(maxfrerror) != 2:
                maxfrerror = [100, 0.2]
            maxfrerror = [float(maxfrerror[0]), float(maxfrerror[1])]
            try:
                eq_max_t = float(cfg.get("max_equilibration_t", 180))
            except Exception:
                eq_max_t = 180.0

            # Start experiment
            active_lines = sorted({int(x) for x in runtime_line_assignment[:len(runtime_lipid_stocks)]})
            active_channels = [1] + [int(line) + 1 for line in runtime_line_assignment[:len(runtime_lipid_stocks)]]
            if line3_constant_on and line3_constant_rate > 0 and not extra_rna_controller_connected:
                active_channels = sorted(set(active_channels + [4]))
            
            # Build comprehensive experiment parameters for record keeping
            exp_params = {
                "volume": next_exp.volume,
                "eq_max_t": float(eq_max_t),
                "expul_t": float(expul_t),
                "maxfrerror": list(maxfrerror),
                "start_comp_idx": start_comp_idx,
                "comp_status": next_exp.comp_status,  # Pass composition status
                "output_wells": next_exp.output_wells,
                "active_channels": active_channels,
                "active_lines": active_lines,
                # Add experiment metadata for record keeping
                "exp_name": next_exp.name,
                "details": f"TFR: {next_exp.tfr}, FRR: {next_exp.frr}",
                "buffer": next_exp.buffer,
                "buffer_notes": "",
                "lipid_stocks": runtime_lipid_stocks,
                "lipid_notes": [""] * len(runtime_lipid_stocks),
                "compositions": runtime_compositions,
                "flow_rates": runtime_flow_rates,
                "flow_rates_exec": runtime_exec_flow_rates,
                "inst_name": str(cfg.get("id", "")),
                "sensorcorr": self.microfluidic_ctrl.sensorcorr,
                "repeats": next_exp.repeats,
                "period": self.microfluidic_ctrl.period,
                "K_p": self.microfluidic_ctrl.K_p,
                "K_i": self.microfluidic_ctrl.K_i,
                "p_incr": self.microfluidic_ctrl.p_incr,
                "p_range": self.microfluidic_ctrl.p_range,
                "tfr": next_exp.tfr,
                "frr": next_exp.frr,
                "screen_space_mode": next_exp.screen_space_mode,
                "screen_space_params": next_exp.screen_space_params,
                "line3_constant_flow_enabled": bool(line3_constant_on),
                "line3_constant_flow_rate": float(line3_constant_rate if line3_constant_on else 0.0),
                # Snapshot of app-level config used for this run (for records/debugging)
                "app_config": dict(cfg),
                "runtime_line_remap_enabled": bool(enable_dynamic_line_remap),
                "runtime_slot_perm_new_to_old": list(runtime_perm),
                "runtime_slot_to_line": list(runtime_line_assignment),
                "original_lipid_stocks": [dict(x) if isinstance(x, dict) else x for x in original_lipid_stocks_with_codes],
            }
            
            self.microfluidic_ctrl.queue_experiment(
                next_exp.exp_id,
                runtime_exec_flow_rates,
                exp_params,
            )

            # Allow a grace period for the microfluidic thread to pick up the command
            start_deadline = time.time() + 10.0
            while time.time() < start_deadline:
                status = self.status_broker.get_status()
                if status.get("current_experiment") == next_exp.exp_id:
                    break
                time.sleep(0.2)
            # Persist assigned log/record number for GUI display (same session and after restart).
            rec_lookup_deadline = time.time() + 5.0
            while time.time() < rec_lookup_deadline:
                rec_id = self.data_logger.get_record_number(next_exp.exp_id)
                if rec_id is not None:
                    try:
                        self.queue_manager.set_record_id(next_exp.exp_id, int(rec_id))
                    except Exception:
                        pass
                    break
                time.sleep(0.1)

            # Wait for completion
            while not self._queue_stop.is_set():
                status = self.status_broker.get_status()
                if status["current_experiment"] is None and status["microfluidic_state"] in ("Idle", "Stopped", "FailSafe"):
                    print(f"[ControlAPI] Experiment finished, checking completion status...")
                    # Reload experiment to get composition status
                    queue = self.queue_manager.get_queue()
                    current_exp = next((e for e in queue if e.exp_id == next_exp.exp_id), None)
                    
                    if current_exp:
                        # Count completed compositions
                        completed_count = sum(1 for cs in current_exp.comp_status if cs == "completed")
                        total_count = len(current_exp.comp_status)
                        print(f"[ControlAPI] Composition status: {completed_count}/{total_count} completed")
                        print(f"[ControlAPI] comp_status list: {current_exp.comp_status}")
                        
                        if completed_count == total_count and total_count > 0:
                            # All compositions completed successfully
                            print(f"[ControlAPI] All compositions done for {next_exp.exp_id}. Marking as completed...")
                            self.queue_manager.update_status(next_exp.exp_id, "completed")
                            print(f"[ControlAPI] Successfully marked {next_exp.exp_id} as completed ({completed_count}/{total_count} compositions)")
                            
                            # Verify it was actually updated
                            verification = self.queue_manager.get_experiment(next_exp.exp_id)
                            if verification:
                                print(f"[ControlAPI] Verification: {next_exp.exp_id} status is now '{verification.status}'")
                            else:
                                print(f"[ControlAPI] WARNING: Could not verify {next_exp.exp_id} status update!")
                            
                            self.set_status("Idle", f"Experiment {next_exp.name} completed")
                        elif status["microfluidic_state"] == "FailSafe":
                            # System entered fail-safe mode (error)
                            self.queue_manager.update_status(next_exp.exp_id, "error", 
                                                            error=f"Fail-safe triggered - only {completed_count}/{total_count} compositions completed")
                            self.set_status("Error", f"{next_exp.name} failed (failsafe)")
                        elif completed_count > 0:
                            # Partial completion (stopped mid-experiment)
                            self.queue_manager.update_status(next_exp.exp_id, "stopped", 
                                                            error=f"Stopped at composition {completed_count + 1}/{total_count}")
                            self.set_status("Stopped", f"{next_exp.name} stopped at {completed_count + 1}/{total_count}")
                        else:
                            # Never started or failed immediately
                            # Only mark as failed if we exceeded the startup grace period
                            if time.time() > start_deadline:
                                self.queue_manager.update_status(next_exp.exp_id, "error", 
                                                                error="Experiment failed to start or failed immediately")
                                self.set_status("Error", f"{next_exp.name} failed to start")
                    break  # Exit inner wait loop, continue to next experiment in queue
                time.sleep(0.5)

            # Optional pause before starting the next experiment
            if not self._queue_stop.is_set():
                queue = self.queue_manager.get_queue()
                has_pending = any(e.status in ("pending", "stopped", "paused") for e in queue)
                if has_pending:
                    wait_s = float(self._app_config.get("inter_experiment_wait_s", 5.0))
                    if wait_s > 0:
                        self.set_status("Idle", f"Waiting {wait_s:.0f}s before next experiment")
                        time.sleep(wait_s)

    def _save_last_used(self):
        self.config_mgr.save_config("app", "last_used", self._last_used)

    def _on_composition_complete(self, exp_id: str, comp_idx: int) -> None:
        """Called when a composition completes successfully."""
        queue = self.queue_manager.get_queue()
        exp = next((e for e in queue if e.exp_id == exp_id), None)
        if exp and comp_idx < len(exp.comp_status):
            exp.comp_status[comp_idx] = "completed"
            self.queue_manager._save_queue_to_disk()
            print(f"[ControlAPI] Composition {comp_idx + 1}/{len(exp.compositions)} marked complete for {exp_id}")
            
            # Check if all compositions are now complete
            completed_count = sum(1 for cs in exp.comp_status if cs == "completed")
            if completed_count == len(exp.comp_status):
                print(f"[ControlAPI] All {completed_count} compositions complete - marking experiment as completed")
                self.queue_manager.update_status(exp_id, "completed")
            
            # Mark the well as complete (opaque) in the output plate visualization
            if comp_idx < len(exp.output_wells):
                well = exp.output_wells[comp_idx]
                if self.gui:
                    try:
                        self.gui.output_plate_widget.mark_well_complete(well[0], well[1], well[2])
                    except Exception as e:
                        print(f"[ControlAPI] Could not update well visualization: {e}")
    
    def _on_experiment_complete(self, exp_id: str):
        """Callback when entire experiment completes - mark as completed immediately."""
        try:
            print(f"[ControlAPI] Experiment {exp_id} completed callback - marking as completed")
            self.queue_manager.update_status(exp_id, "completed")
            try:
                self.data_logger.finalize_experiment(exp_id, {"status": "completed"})
            except Exception as e:
                print(f"[ControlAPI] Warning: could not finalize experiment records for {exp_id}: {e}")
            
            # Verify
            exp = self.queue_manager.get_experiment(exp_id)
            if exp:
                print(f"[ControlAPI] Verified: {exp_id} status is now '{exp.status}'")
            else:
                print(f"[ControlAPI] WARNING: Could not verify {exp_id} after marking complete")
        except Exception as e:
            print(f"[ControlAPI] Error in _on_experiment_complete: {e}")

    def connect_microfluidics(
        self,
        config_name,
        calibration,
        sensor_config,
        connect_extra_pressure: bool = False,
        extra_pressure_com_port: str = "",
        extra_config_name: str = "",
        extra_calibration: str = "load",
    ):
        try:
            cfg = self.config_mgr.load_config("microfluidics", config_name) or {}
            extra_cfg = self.config_mgr.load_config("microfluidics", extra_config_name) if extra_config_name else None
            self.microfluidic_ctrl.initialize(
                sensor_config=sensor_config,
                calibration=calibration,
                device_id=str(cfg.get("id", "")),
                connect_extra_pressure=False,
                extra_pressure_com_port="",
            )
            self.status_broker.set_connection_state("microfluidics", True)
            extra_msg = ""
            if bool(connect_extra_pressure):
                try:
                    extra_port = str(extra_pressure_com_port or (extra_cfg or {}).get("port", "COM6"))
                    extra_device = str((extra_cfg or {}).get("id", extra_config_name or "Mk4_Extra"))
                    extra_cal = str(extra_calibration or "load")
                    print(
                        "[ControlAPI] Connecting extra pressure controller: "
                        f"config={extra_config_name}, device={extra_device}, port={extra_port}, calibration={extra_cal}",
                        flush=True,
                    )
                    self.microfluidic_ctrl._connect_extra_pump(
                        extra_port,
                        extra_device,
                        extra_cal,
                    )
                    extra_msg = "Extra Mk4 pump connected."
                    print("[ControlAPI] Extra pressure controller connected successfully.", flush=True)
                except Exception as e:
                    self.microfluidic_ctrl.extra_pump_connected = False
                    self.status_broker.set_connection_state("extra_pressure", False)
                    extra_msg = f"Extra Mk4 not connected: {e}"
                    print(f"[ControlAPI] Extra pressure controller connection failed: {e}", flush=True)
            self._last_used["microfluidics"] = {
                "config": config_name,
                "calibration": calibration,
                "connect_extra_pressure": bool(connect_extra_pressure),
                "extra_pressure_com_port": str(extra_pressure_com_port or ""),
                "extra_config": str(extra_config_name or ""),
                "extra_calibration": str(extra_calibration or "load"),
            }
            self._save_last_used()
            return True, extra_msg
        except Exception as e:
            self.status_broker.set_error(f"Microfluidics connect failed: {e}")
            return False, str(e)

    def is_extra_pressure_connected(self) -> bool:
        try:
            return bool(getattr(self.microfluidic_ctrl, "extra_pump_connected", False))
        except Exception:
            return False

    def connect_microcontroller(self, port_name, secondary_port_name: str = ""):
        try:
            resume = self.has_checkpoint()
            self.microfluidic_ctrl.init_microcontroller(port_name, keep_servo_state=resume)
            secondary_msg = ""
            secondary_port_name = (secondary_port_name or "").strip()
            if secondary_port_name and secondary_port_name.lower() not in ("none", "disabled"):
                if secondary_port_name == port_name:
                    secondary_msg = "Secondary microcontroller ignored (same COM as main)."
                else:
                    try:
                        self.microfluidic_ctrl.init_secondary_microcontroller(
                            secondary_port_name,
                            keep_servo_state=resume,
                        )
                        secondary_msg = f" Secondary connected on {secondary_port_name}."
                    except Exception as se:
                        secondary_msg = f" Secondary not connected: {se}"
            self._ensure_robot_loader()
            if not resume:
                # Initialize all servo valves to 80 degrees (open) in background
                threading.Thread(target=self._initialize_servo_valves, daemon=True).start()
            self.status_broker.set_connection_state("microcontroller", True)
            self._last_used["microcontroller"] = {"port": port_name, "secondary_port": secondary_port_name}
            self._save_last_used()
            return True, ("Main microcontroller connected." + secondary_msg).strip()
        except Exception as e:
            self.status_broker.set_error(f"Microcontroller connect failed: {e}")
            return False, str(e)
    
    def _initialize_servo_valves(self):
        """Initialize servo valves to safe startup positions on connection."""
        try:
            import expel
            import time
            ser_main = self.microfluidic_ctrl.ser
            ser_secondary = getattr(self.microfluidic_ctrl, "ser_secondary", None)
            if ser_main:
                # Wait for serial port to be ready
                time.sleep(0.5)
                
                for line in (1, 2, 3):
                    try:
                        # Line valves 1-3 closed by default.
                        expel.set_servo_angle(ser_main, line, 40)
                        time.sleep(0.2)
                        # Chip valve: neutral/open
                        expel.set_servo_angle(ser_main, line + 3, 80)
                        time.sleep(0.2)
                        print(f"[ControlAPI] Initialized servo angles for line {line}")
                    except Exception as e:
                        print(f"[ControlAPI] Error initializing line {line} servos: {e}")
                        self.set_status("Warning", f"Could not initialize line {line} servos: {str(e)}")
                
            if ser_secondary:
                for servo_num in (7, 8, 9):
                    try:
                        expel.set_servo_angle(ser_secondary, servo_num, 80)
                        time.sleep(0.2)
                        print(f"[ControlAPI] Initialized secondary servo {servo_num}")
                    except Exception as e:
                        print(f"[ControlAPI] Error initializing secondary servo {servo_num}: {e}")
                        self.set_status("Warning", f"Could not initialize secondary servo {servo_num}: {str(e)}")

            self.set_status("Ready", "Servo valves initialized (L1-3=40°, L4-6=80°)")
        except Exception as e:
            print(f"[ControlAPI] Error initializing servo valves: {e}")
            self.set_status("Warning", f"Could not initialize servo valves: {str(e)}")

    def connect_dobot(self, config_name, calib_file):
        try:
            cfg = self.config_mgr.load_config("dobot", config_name) or {}
            ip = cfg.get("ip")
            port = int(cfg.get("port", 0))
            if not ip or not port:
                raise ValueError("Invalid Dobot config")

            retries = 2
            last_err = None
            for _ in range(retries):
                try:
                    client = RobotClient(
                        ip, port,
                        timeout_s=5.0,
                        max_retries=2,
                        retry_delay_s=0.25,
                        backoff=2.0
                    )
                    client.connect()
                    self.robot_sequencer.set_dobot_client(client)
                    if calib_file:
                        self.robot_sequencer.set_plate_calibration(calib_file)
                    self.robot_sequencer.start()
                    self._ensure_robot_loader()
                    self.status_broker.set_connection_state("dobot", True)
                    self._last_used["dobot"] = {"config": config_name, "calib_file": calib_file}
                    self._save_last_used()
                    return True, ""
                except Exception as e:
                    last_err = e
            raise RuntimeError(f"Dobot connect failed: {last_err}")
        except Exception as e:
            self.status_broker.set_error(f"Dobot connect failed: {e}")
            return False, str(e)

    def add_experiment_to_queue(self, exp_data):
        exp_data = dict(exp_data or {})
        line3_enabled = bool(exp_data.get("line3_constant_flow_enabled", False))
        exp_data["line3_uses_main_pump"] = bool(line3_enabled and not self.is_extra_pressure_connected())
        # Validate: no duplicate lipids in experiment
        lipid_names = [l["name"] for l in exp_data.get("lipid_stocks", [])]
        if len(lipid_names) != len(set(lipid_names)):
            raise ValueError("Experiment cannot have duplicate lipids on different lines")
        
        exp = self.queue_manager.add_experiment(exp_data)
        self._update_plate_map_for_experiment(exp)
        return exp

    def import_experiments_from_csv(self, csv_path: str) -> Tuple[bool, str]:
        """
        Import mixed-lipid compositions from CSV and create queue experiments.

        Required columns (case-insensitive):
          line1_code,line1_comp,line2_code,line2_comp,line3_code,line3_comp,tfr,frr,volume,repeats

        `line*_code` may be empty or "None" to disable that line.
        Lipid codes must exist in lipid library (`lipid_code` field in lipid config).
        """
        if not csv_path or not os.path.exists(csv_path):
            return False, "CSV file not found."

        buffer_name = self._buffer_selected_name
        if not buffer_name:
            buffer_names = self.get_buffer_configs()
            if buffer_names:
                buffer_name = buffer_names[0]
            else:
                buffer_name = "Buffer"
        buffer_cfg = self.config_mgr.load_config("buffers", buffer_name) or {}
        buffer = {
            "name": buffer_name,
            "concentration": buffer_cfg.get("concentration", 0),
        }

        code_to_lipid: Dict[str, Dict] = {}
        for lipid_name in self.get_lipid_configs():
            cfg = self.load_lipid_config(lipid_name) or {}
            code = str(cfg.get("lipid_code", "")).strip().upper()
            if not code:
                continue
            lipid_entry = {
                "name": lipid_name,
                "concentration": cfg.get("concentration", ""),
                "concentration_mM": cfg.get("concentration_mM", ""),
                "mw": cfg.get("mw", ""),
                "units": cfg.get("units", "mM"),
                "color": cfg.get("color", "#777777"),
                "lipid_code": code,
            }
            code_to_lipid[code] = lipid_entry

        if not code_to_lipid:
            return False, "No lipid codes found in lipid library. Add lipid codes first."

        required = {
            "line1_code", "line1_comp",
            "line2_code", "line2_comp",
            "line3_code", "line3_comp",
            "tfr", "frr", "volume", "repeats",
        }

        metadata: Dict[str, str] = {}
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        except Exception as e:
            return False, f"Could not read CSV: {e}"

        header_idx = None
        for i, line in enumerate(lines):
            if "line1_code" in line.strip().lower():
                header_idx = i
                break
        if header_idx is None:
            return False, "CSV header not found (expected line containing 'line1_code')."

        for raw in lines[:header_idx]:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line[1:].strip()
            if ":" in line:
                k, v = line.split(":", 1)
            elif "=" in line:
                k, v = line.split("=", 1)
            elif "," in line:
                k, v = line.split(",", 1)
            else:
                continue
            metadata[k.strip().lower()] = v.strip()

        try:
            csv_payload = "\n".join(lines[header_idx:])
            reader = csv.DictReader(io.StringIO(csv_payload))
            if not reader.fieldnames:
                return False, "CSV has no header row."
            keymap = {h.strip().lower(): h for h in reader.fieldnames}
            missing = [k for k in required if k not in keymap]
            if missing:
                return False, f"CSV missing required columns: {', '.join(missing)}"
            raw_rows = list(reader)
        except Exception as e:
            return False, f"Could not parse CSV rows: {e}"

        if not raw_rows:
            return False, "CSV contains no data rows."

        parsed_rows = []
        for idx, row in enumerate(raw_rows, start=1):
            try:
                row_get = lambda k: str(row.get(keymap[k], "")).strip()
                line_codes = []
                line_comps = []
                for line in (1, 2, 3):
                    code_raw = row_get(f"line{line}_code").upper()
                    if code_raw in ("", "NONE", "NULL", "N/A", "-"):
                        code_raw = ""
                    comp_raw = row_get(f"line{line}_comp")
                    comp_val = float(comp_raw) if comp_raw != "" else 0.0
                    if comp_val < 0:
                        return False, f"Row {idx}: line{line}_comp cannot be negative."
                    line_codes.append(code_raw)
                    line_comps.append(comp_val)

                active = [(c, p) for c, p in zip(line_codes, line_comps) if c]
                if not active:
                    return False, f"Row {idx}: at least one lipid code is required."

                # Validate codes and duplicates
                active_codes = [c for c, _ in active]
                if len(active_codes) != len(set(active_codes)):
                    return False, f"Row {idx}: duplicate lipid codes on multiple lines are not allowed."
                for c in active_codes:
                    if c not in code_to_lipid:
                        return False, f"Row {idx}: lipid code '{c}' not found in lipid library."

                comp_sum = sum(p for _, p in active)
                if abs(comp_sum - 100.0) > 1.0:
                    return False, f"Row {idx}: active line compositions must sum to 100 (got {comp_sum:.3f})."

                tfr = float(row_get("tfr"))
                frr = float(row_get("frr"))
                volume = float(row_get("volume"))
                repeats = int(float(row_get("repeats")))
                if tfr <= 0 or frr <= 0 or volume <= 0 or repeats <= 0:
                    return False, f"Row {idx}: tfr/frr/volume/repeats must be positive."

                compact_codes = [c for c in line_codes if c]
                compact_comps = [p for c, p in zip(line_codes, line_comps) if c]
                parsed_rows.append({
                    "row_idx": idx,
                    "line_codes": tuple(line_codes),            # preserve original 3-line pattern
                    "compact_codes": tuple(compact_codes),      # execution order
                    "compact_comp": list(compact_comps),
                    "tfr": tfr,
                    "frr": frr,
                    "volume": volume,
                    "repeats": repeats,
                })
            except Exception as e:
                return False, f"Row {idx}: parse error: {e}"

        # Group rows that can run in one experiment (same lipid set and run parameters)
        grouped: Dict[Tuple, Dict] = {}
        for item in parsed_rows:
            key = (
                item["compact_codes"],
                round(item["tfr"], 9),
                round(item["frr"], 9),
                round(item["volume"], 9),
                item["repeats"],
            )
            if key not in grouped:
                grouped[key] = {
                    "compact_codes": item["compact_codes"],
                    "line_signature": item["line_codes"],
                    "tfr": item["tfr"],
                    "frr": item["frr"],
                    "volume": item["volume"],
                    "repeats": item["repeats"],
                    "rows": [],
                }
            grouped[key]["rows"].append(item)

        groups = list(grouped.values())

        # Min-switch ordering between groups (greedy on line signature distance)
        # Distance = number of line positions (1..3) where lipid code differs.
        def _dist(sig_a: Tuple[str, str, str], sig_b: Tuple[str, str, str]) -> int:
            return sum(1 for a, b in zip(sig_a, sig_b) if a != b)

        ordered_groups = []
        remaining = groups[:]
        current_sig = ("", "", "")
        while remaining:
            best_idx = 0
            best_cost = None
            for i, g in enumerate(remaining):
                cost = _dist(current_sig, g["line_signature"])
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_idx = i
            chosen = remaining.pop(best_idx)
            ordered_groups.append(chosen)
            current_sig = chosen["line_signature"]

        import_name = str(metadata.get("experiment_name", "")).strip()
        base_name = import_name if import_name else "Imported"
        added = 0
        for i, g in enumerate(ordered_groups, start=1):
            lipid_stocks = [dict(code_to_lipid[c]) for c in g["compact_codes"]]
            compositions = [list(r["compact_comp"]) for r in sorted(g["rows"], key=lambda x: x["row_idx"])]
            line_code_text = "/".join([c if c else "None" for c in g["line_signature"]])
            exp_data = {
                "name": f"{base_name} {i:02d} [{line_code_text}]",
                "buffer": buffer,
                "lipid_stocks": lipid_stocks,
                "tfr": g["tfr"],
                "frr": g["frr"],
                "volume": g["volume"],
                "repeats": g["repeats"],
                "screen_space_mode": "Manual",
                "screen_space_params": {"compositions": compositions},
            }
            self.add_experiment_to_queue(exp_data)
            added += 1

        return True, f"Imported {added} experiments from {len(parsed_rows)} rows (buffer: {buffer['name']})."

    def get_queue(self):
        return self.queue_manager.get_queue()

    def get_experiment_details(self, exp_id):
        exp = self.queue_manager.get_experiment(exp_id)
        if not exp:
            return []
        lipid_names = [l["name"] for l in exp.lipid_stocks] if exp.lipid_stocks else []
        buffer_name = exp.buffer.get("name", "") if exp.buffer else ""
        
        # Calculate base composition count (before repeats)
        base_comp_count = len(exp.compositions) // exp.repeats if exp.repeats > 0 else len(exp.compositions)
        
        rows = []
        
        for i, comp in enumerate(exp.compositions):
            fr = exp.flow_rates[i]
            well = exp.output_wells[i]
            
            # Determine which base composition and repeat this is
            base_idx = i // exp.repeats if exp.repeats > 0 else i
            repeat_num = (i % exp.repeats) + 1 if exp.repeats > 0 else 1
            
            lipid_status = []
            commands = []
            allocation = {}
            is_runnable = True
            
            for idx, lipid in enumerate(exp.lipid_stocks):
                lipid_name = lipid["name"]
                line_idx = idx + 1
                
                if line_idx > 3:
                    lipid_status.append(f"✗ {lipid_name} (no line available)")
                    is_runnable = False
                    continue
                
                # Check state 2: is it allocated to intake?
                source_wells = self.lipid_manager.find_intake_wells_with_lipid(lipid_name)
                
                if not source_wells:
                    lipid_status.append(f"✗ {lipid_name} (not in intake)")
                    is_runnable = False
                else:
                    source_well = source_wells[0]
                    lipid_status.append(f"✓ {lipid_name} (from {source_well} → Line {line_idx})")
                    allocation[lipid_name] = (line_idx, source_well)
                    commands.append(f"Load {lipid_name} from {source_well} → Line {line_idx}")
            
            status_text = exp.comp_status[i]
            if exp.comp_status[i] == "pending":
                if is_runnable:
                    if commands:
                        repeat_info = f" [Repeat {repeat_num}/{exp.repeats}]" if exp.repeats > 1 else ""
                        status_text = f"Ready{repeat_info} | Prep: {'; '.join(commands)}"
                    else:
                        status_text = "Ready"
                else:
                    status_text = "⚠ Missing lipid in intake"
            
            # Add repeat number to composition display
            comp_display = comp + ([f"R{repeat_num}"] if exp.repeats > 1 else [])
            
            rows.append({
                "buffer": buffer_name,
                "lipids": lipid_names,
                "composition": comp,
                "composition_display": comp_display,
                "repeat_num": repeat_num,
                "total_repeats": exp.repeats,
                "base_comp_idx": base_idx,
                "flow_rates": [round(float(x), 2) for x in fr],
                "well": tuple(well),
                "status": exp.comp_status[i],
                "plot_link": exp.plot_links[i] or "",
                "lipid_availability": " | ".join(lipid_status) if lipid_status else "N/A",
                "detailed_status": status_text,
                "allocation": allocation,
                "is_runnable": is_runnable,
            })
        return rows

    def set_intake_lipid(self, plate, row, col, lipid_name, color_hex):
        """Add lipid to intake well (state 2)."""
        self.lipid_manager.allocate_to_intake(plate, row, col, lipid_name)
        self.config_mgr.save_config("lipid_colors", lipid_name, {"color": color_hex})
        
        if self.gui:
            self.gui._refresh_queue_table()
        
        return True

    def clear_intake_lipid(self, plate, row, col):
        """Remove lipid from intake well (state 2)."""
        self.lipid_manager.remove_from_intake(plate, row, col)
        if self.gui:
            self.gui._refresh_queue_table()
        return True

    def get_plate_state(self):
        return self.plate_tracker.get_plate

    def skip_lipid_experiments(self, lipid_name):
        self.queue_manager.mark_experiments_red(lipid_name)

    def _update_plate_map_for_experiment(self, exp):
        """Update plate visualization with experiment wells (preview mode - transparent)."""
        lipid_colors = {}
        for lipid in exp.lipid_stocks:
            cfg = self.config_mgr.load_config("lipid_colors", lipid["name"]) or {}
            lipid_colors[lipid["name"]] = cfg.get("color", "#777777")

        for i, comp in enumerate(exp.compositions):
            well = exp.output_wells[i]
            color = self._blend_color(comp, exp.lipid_stocks, lipid_colors)
            self.plate_tracker.set_well_color(well[0], well[1], well[2], color)
            
            # Also update GUI if available (show as preview - semi-transparent)
            if self.gui:
                try:
                    # Check if this composition has been completed
                    is_preview = exp.comp_status[i] != "completed" if i < len(exp.comp_status) else True
                    self.gui.output_plate_widget.set_well_color(well[0], well[1], well[2], color, is_preview=is_preview)
                except Exception as e:
                    print(f"[ControlAPI] Could not update GUI well visualization: {e}")

    def _blend_color(self, comp, lipid_stocks, lipid_colors):
        """Blend lipid colors based on composition percentages. Only uses lipid colors, not buffer.
        
        Args:
            comp: List of lipid molar percentages (e.g. [50, 30, 20] for 3 lipids)
            lipid_stocks: List of loaded lipid dicts with 'name' field
            lipid_colors: Dict mapping lipid names to hex colors
        
        Returns:
            Hex color string blended from lipids only
        """
        if not lipid_stocks or not comp:
            return "#777777"
        
        # Only use lipid percentages - composition should contain lipid mol% values
        num_lipids = min(len(lipid_stocks), len(comp))
        if num_lipids == 0:
            return "#777777"
        
        # Sum only the lipid percentages (should sum to 100 if valid)
        total = max(sum(comp[:num_lipids]), 1)
        
        rgb = [0, 0, 0]
        for i in range(num_lipids):
            lipid = lipid_stocks[i]
            # Get lipid color, default to gray if not found
            color = lipid_colors.get(lipid["name"], "#777777").lstrip("#")
            try:
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
            except (ValueError, IndexError):
                # Invalid color format, use gray
                r = g = b = 119
            
            # Weight by composition percentage
            w = comp[i] / total
            rgb[0] += r * w
            rgb[1] += g * w
            rgb[2] += b * w
        
        return "#{:02X}{:02X}{:02X}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))

    def _ensure_lipid_loaded(self, line_idx, lipid_name, assume_cleaned: bool = False):
        # State 3: check if already loaded on line
        line = self.lipid_manager.get_line_state(line_idx)
        if line["lipid_name"] == lipid_name and line["remaining_volume"] > 0:
            self.status_broker.set_line_status(line_idx, "Already loaded")
            return True

        # State 2: find intake well with this lipid
        source_wells = self.lipid_manager.find_intake_wells_with_lipid(lipid_name)
        if not source_wells:
            self.status_broker.set_line_status(line_idx, "Load failed: missing intake")
            self._last_load_failure = {
                "type": "missing_intake",
                "line": int(line_idx),
                "lipid": str(lipid_name),
                "message": f"Missing lipid in intake for line {line_idx}: {lipid_name}",
            }
            return False

        source_well = source_wells[0]
        
        # Clean if switching
        if (not assume_cleaned) and line["lipid_name"] and line["lipid_name"] != lipid_name:
            self.robot_sequencer.queue_clean_line(line_idx)
            self.status_broker.set_line_status(line_idx, "Cleaning")

        # Load from intake to line (state 2 -> state 3)
        self._loaded_lines_this_exp.add(line_idx)
        self.status_broker.set_line_status(line_idx, "Loading")
        self.set_status("Loading", f"Line {line_idx}: {lipid_name}")
        self.robot_sequencer.queue_load_lipid(line_idx, source_well[0], source_well[1], source_well[2], lipid_name)
        return self._wait_for_line_loaded(line_idx, lipid_name, source_well)

    def _wait_for_line_loaded(self, line_idx: int, lipid_name: str, source_well: tuple, timeout_s: float = 600.0) -> bool:
        """Block until the specified line is loaded with the requested lipid."""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout_s:
            line = self.lipid_manager.get_line_state(line_idx)
            if line["lipid_name"] == lipid_name and line["remaining_volume"] > 0:
                self.status_broker.set_line_status(line_idx, "Loaded")
                return True
            with self._recovery_lock:
                recovery_active = self._dobot_recovery_active
                recovery_ctx = dict(self._dobot_recovery_context or {})
            if recovery_active and int(recovery_ctx.get("line") or 0) == int(line_idx):
                self.set_status("Paused", f"Waiting for Dobot reconnect (line {line_idx})")
                if not self._wait_for_recovery_continue():
                    self.status_broker.set_line_status(line_idx, "Paused: reconnect required")
                    return False
                self.set_status("Loading", f"Retrying line {line_idx} from start")
                self.status_broker.set_line_status(line_idx, "Retrying load")
                self.robot_sequencer.queue_load_lipid(
                    line_idx,
                    int(source_well[0]),
                    int(source_well[1]),
                    int(source_well[2]),
                    lipid_name,
                )
                start = time.monotonic()
            time.sleep(0.2)
        self.status_broker.set_error(f"Timeout waiting for line {line_idx} to load {lipid_name}")
        self.status_broker.set_line_status(line_idx, "Load timeout")
        self._last_load_failure = {
            "type": "load_timeout",
            "line": int(line_idx),
            "lipid": str(lipid_name),
            "message": f"Timeout waiting for line {line_idx} to load {lipid_name}",
        }
        return False

    def get_config(self):
        return dict(self._app_config)

    def set_config(self, cfg: dict):
        self._app_config.update(cfg)
        self.config_mgr.save_config("app", "config", self._app_config)
        try:
            self.robot_sequencer.set_remove_stoppers_enabled(
                bool(self._app_config.get("Remove Stoppers", False))
            )
            loader = getattr(self.robot_sequencer, "robot_loader", None)
            if loader:
                loader.load_flush_through_chip = bool(self._app_config.get("load_flush_through_chip", False))
                loader.wash_cycles = max(1, int(float(self._app_config.get("wash_cycles", 1))))
                loader.cleaning_flush_pressure_mbar = float(self._app_config.get("cleaning_flush_pressure_mbar", 70.0))
        except Exception:
            pass

    # --- Config presets ---
    def list_config_presets(self) -> List[str]:
        """List saved config presets."""
        return self.config_mgr.list_configs("app_presets")

    def delete_config_preset(self, name: str) -> None:
        """Delete a named config preset."""
        self.config_mgr.delete_config("app_presets", name)
        if self._selected_config_preset == name:
            self._selected_config_preset = None
            self.config_mgr.save_config("app", "selected_config", {"name": ""})

    def save_config_preset(self, name: str) -> None:
        """Save current app config as a named preset."""
        self.config_mgr.save_config("app_presets", name, dict(self._app_config))

    def load_config_preset(self, name: str) -> dict:
        """Load a named config preset (does not auto-apply)."""
        return self.config_mgr.load_config("app_presets", name) or {}

    def apply_config_preset(self, name: str) -> Tuple[bool, str]:
        """Load and apply a named config preset."""
        cfg = self.load_config_preset(name)
        if not cfg:
            return False, f"Config preset '{name}' not found."
        self.set_config(cfg)
        return True, ""

    def get_selected_config_preset(self) -> str:
        """Return the currently selected named config preset."""
        return str(self._selected_config_preset or "")

    def select_config_preset(self, name: str) -> Tuple[bool, str]:
        """Select a named config preset and apply it as active app config."""
        try:
            name = str(name or "").strip()
            if not name:
                return False, "Config name is required."
            cfg = self.load_config_preset(name)
            if not cfg:
                return False, f"Config preset '{name}' not found."
            self.set_config(cfg)
            self._selected_config_preset = name
            self.config_mgr.save_config("app", "selected_config", {"name": name})
            return True, ""
        except Exception as e:
            return False, str(e)

    def create_or_update_named_config(self, name: str, cfg: dict) -> Tuple[bool, str]:
        """Save and select a named config preset from provided config values."""
        try:
            name = str(name or "").strip()
            if not name:
                return False, "Config name is required."
            cfg_map = dict(cfg or {})
            self.set_config(cfg_map)
            self.config_mgr.save_config("app_presets", name, cfg_map)
            self._selected_config_preset = name
            self.config_mgr.save_config("app", "selected_config", {"name": name})
            return True, ""
        except Exception as e:
            return False, str(e)

    # --- Experiment presets ---
    def list_experiment_presets(self) -> List[str]:
        """List saved experiment presets."""
        return self.config_mgr.list_configs("experiment_presets")

    def delete_experiment_preset(self, name: str) -> None:
        """Delete a named experiment preset."""
        self.config_mgr.delete_config("experiment_presets", name)

    def save_experiment_preset(self, name: str, exp_id: Optional[str] = None, exp_data: Optional[Dict] = None) -> Tuple[bool, str]:
        """Save an experiment preset by exp_id or raw exp_data."""
        try:
            if exp_data is None:
                if not exp_id:
                    return False, "exp_id or exp_data is required."
                exp = self.queue_manager.get_experiment(exp_id)
                if not exp:
                    return False, f"Experiment '{exp_id}' not found."
                exp_data = {
                    "name": exp.name,
                    "buffer": exp.buffer,
                    "lipid_stocks": list(exp.lipid_stocks),
                    "tfr": exp.tfr,
                    "frr": exp.frr,
                    "volume": exp.volume,
                    "repeats": exp.repeats,
                    "screen_space_mode": exp.screen_space_mode,
                    "screen_space_params": dict(exp.screen_space_params),
                    "output_wells": [list(w) for w in exp.output_wells],
                }
            self.config_mgr.save_config("experiment_presets", name, exp_data)
            return True, ""
        except Exception as e:
            return False, str(e)

    def load_experiment_preset(self, name: str) -> dict:
        """Load a named experiment preset."""
        return self.config_mgr.load_config("experiment_presets", name) or {}

    def add_experiment_from_preset(self, name: str):
        """Add an experiment to the queue from a named preset."""
        exp_data = self.load_experiment_preset(name)
        if not exp_data:
            raise ValueError(f"Experiment preset '{name}' not found.")
        return self.add_experiment_to_queue(exp_data)

    def set_buffer_selected(self, name: str):
        self._buffer_selected_name = name
        self._last_used["buffer"] = {"name": name}
        self._save_last_used()

    def set_start_well(self, plate: int, row: int, col: int) -> None:
        """Set the starting well for the next experiment (before run starts)."""
        self._start_well = (plate, row, col)
        self.status_broker.set_start_well(plate, row, col)

    def set_experiment_start_well(self, exp_id: str, plate: int, row: int, col: int) -> Tuple[bool, str]:
        """Set start well for a specific pending experiment only."""
        try:
            exp = self.queue_manager.get_experiment(str(exp_id))
            if not exp:
                return False, "Experiment not found."
            if getattr(exp, "status", "") != "pending":
                return False, "Only pending experiments can be reassigned."

            p = int(plate)
            r = int(row)
            c = int(col)
            if p < 1 or p > 6 or r < 1 or r > 8 or c < 1 or c > 12:
                return False, "Start well is out of range."

            count = len(getattr(exp, "compositions", []) or [])
            wells = []
            cur_p, cur_r, cur_c = p, r, c
            for _ in range(count):
                if cur_p > 6:
                    return False, "Not enough plate capacity from selected start well."
                wells.append([cur_p, cur_r, cur_c])
                cur_c += 1
                if cur_c > 12:
                    cur_c = 1
                    cur_r += 1
                    if cur_r > 8:
                        cur_r = 1
                        cur_p += 1

            exp.output_wells = wells
            self.queue_manager._save_queue_to_disk()
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_plate_calibration(self) -> Dict:
        """Get per-plate calibration map."""
        return dict(self._plate_calibration)

    def set_plate_calibration(self, plate: int, steps_h: int, steps_v: int) -> None:
        """Set and persist calibration for a plate (first well position)."""
        self._plate_calibration[str(plate)] = {
            "stepsH": int(steps_h),
            "stepsV": int(steps_v),
        }
        self.config_mgr.save_config("app", "plate_calibration", self._plate_calibration)
        self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)

    def home_stage_to_plate(self, plate: int) -> None:
        """Home stage and move to plate start if calibrated."""
        self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)
        self.microfluidic_ctrl.home_to_plate(plate)

    def jog_stage(self, dir_h: str, dir_v: str, steps_h: int, steps_v: int) -> None:
        """Jog stage by steps."""
        self.microfluidic_ctrl.jog_stage(dir_h, dir_v, steps_h, steps_v)

    def admin_home_stage_to_plate(self, plate: int) -> Tuple[bool, str]:
        """Admin: home stage and track current well as A1 on selected plate."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before manual stage movement."
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual movement."
            self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)
            self.microfluidic_ctrl.home_to_plate(plate)
            self.microfluidic_ctrl.set_manual_current_well((plate, 1, 1))
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_move_to_well(self, plate: int, row: int, col: int, *, rehome: bool = False) -> Tuple[bool, str]:
        """Admin: move stage to a specific well for testing."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before manual stage movement."
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual movement."
            self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)
            self.microfluidic_ctrl.move_to_well((plate, row, col), rehome=rehome)
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_set_servo_position(self, servo_number: int, angle: int) -> Tuple[bool, str]:
        """Admin: set rotary servo position."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            import expel
            servo_number = int(servo_number)
            angle = int(angle)
            if servo_number < 1 or servo_number > 9:
                return False, "Servo must be between 1 and 9."

            if servo_number <= 6:
                ser = self.microfluidic_ctrl.ser
                if not ser:
                    return False, "Main microcontroller is not connected."
            else:
                ser = getattr(self.microfluidic_ctrl, "ser_secondary", None)
                if not ser:
                    return False, "Secondary microcontroller is not connected."
                # Secondary MCU wiring compensation: logical 8/9 are swapped physically.
                if servo_number == 8:
                    servo_number = 9
                elif servo_number == 9:
                    servo_number = 8

            expel.set_servo_angle(ser, servo_number, angle)
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_prime_lines(self, loaded_lines: List[int]) -> Tuple[bool, str]:
        """Admin: prime selected lines."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            cfg = self.get_config() or {}
            line3_mode_on = bool(cfg.get("line3_RNA_constant", cfg.get("line3_constant_mode_enabled", False)))
            prime_extra_rna = bool(line3_mode_on and self.is_extra_pressure_connected())
            prime_line3_as_rna = bool(
                line3_mode_on
                and not self.is_extra_pressure_connected()
                and 3 in {int(x) for x in (loaded_lines or [])}
            )
            self.microfluidic_ctrl.prime_lines(
                list(loaded_lines),
                line3_constant_active=prime_line3_as_rna,
                extra_rna_active=prime_extra_rna,
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_set_dobot_valve(self, line: int, state: str) -> Tuple[bool, str]:
        """Admin: manually set a Dobot valve on/off."""
        try:
            if state not in ("on", "off"):
                return False, "Valve state must be 'on' or 'off'."
            if line not in (1, 2, 3):
                return False, "Line must be 1, 2, or 3."

            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual valve control."

            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."

            # Give any queued robot actions a moment to finish
            try:
                self.robot_sequencer.wait_until_idle(timeout_s=5.0)
            except Exception:
                pass

            if state == "on":
                # Safety interlock: force low pressure before enabling dobot valve path.
                try:
                    import pump
                    pump.set_pressure(line + 1, 0, self.microfluidic_ctrl.calibarr)
                    time.sleep(0.1)
                except Exception:
                    pass

            from robot_loader import set_output
            set_output(self.robot_sequencer.dobot_client, line + 8, state, timeout_s=0.5, safe_to_retry=False)
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_set_channel_pressures(self, p1: float, p2: float, p3: float, p4: float) -> Tuple[bool, str]:
        """Admin: manually set pressure setpoints (mbar) for channels 1..4."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."

            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual pressure control."

            calibarr = getattr(self.microfluidic_ctrl, "calibarr", None)
            if calibarr is None:
                return False, "Pressure calibration is not available."

            vals = [float(p1), float(p2), float(p3), float(p4)]
            for i, v in enumerate(vals, start=1):
                if v < 0 or v > 2000:
                    return False, f"Channel {i} pressure must be between 0 and 2000 mbar."

            import pump
            for i, v in enumerate(vals, start=1):
                pump.set_pressure(i, v, calibarr)

            self.set_status(
                "Ready",
                f"Manual pressures set: C1={vals[0]:.1f}, C2={vals[1]:.1f}, C3={vals[2]:.1f}, C4={vals[3]:.1f} mbar",
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_read_all_sensors(self) -> Tuple[bool, str]:
        """Admin: read all sensors and print raw/corrected flows to terminal."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            self.microfluidic_ctrl.read_all_sensor_flows()
            return True, "Sensor diagnostic printed to terminal."
        except Exception as e:
            return False, str(e)

    def admin_get_sensor_snapshot(self):
        """Admin: get raw/corrected sensor snapshot for monitoring UI."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            rows = self.microfluidic_ctrl.get_sensor_snapshot()
            return True, rows
        except Exception as e:
            return False, str(e)

    def admin_get_extra_pressure_snapshot(self):
        """Admin: get extra pump connection and latest reading."""
        try:
            ctrl = self.microfluidic_ctrl
            connected = bool(getattr(ctrl, "extra_pump_connected", False))
            flow_val = None
            last_p = float(getattr(ctrl, "extra_pressure_last", 0.0) or 0.0)
            actual_p = None
            if connected:
                try:
                    flow_val = ctrl._read_extra_flow()
                except Exception:
                    flow_val = getattr(ctrl, "extra_flow_last", None)
                try:
                    actual_p, _ = ctrl.extra_pump.get_pressure()
                except Exception:
                    actual_p = None
            return True, {"connected": connected, "flow": flow_val, "pressure_set": last_p, "pressure_actual": actual_p}
        except Exception as e:
            return False, str(e)

    def admin_set_extra_pressure(self, pressure_mbar: float) -> Tuple[bool, str]:
        """Admin: manually set extra-pump pressure."""
        try:
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual extra pressure control."

            ctrl = self.microfluidic_ctrl
            if not bool(getattr(ctrl, "extra_pump_connected", False)):
                return False, "Extra pressure controller is not connected."

            p = float(pressure_mbar)
            pmin = float(getattr(ctrl.extra_pump, "pressure_min", 0.0) or 0.0)
            pmax = float(getattr(ctrl.extra_pump, "pressure_max", 1000.0) or 1000.0)
            if p < pmin or p > pmax:
                return False, f"Pressure must be between {pmin:.1f} and {pmax:.1f} mbar."

            ok, err = ctrl.extra_pump.set_pressure(p)
            if not ok:
                return False, err or "Failed to set extra pressure."
            ctrl.extra_pressure_last = p
            self.set_status("Ready", f"Extra pressure set: {p:.1f} mbar")
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_move_dobot_to_intake_hover(self, plate: int, row: int, col: int) -> Tuple[bool, str]:
        """Admin: move Dobot above a selected intake well (no pick/place)."""
        try:
            plate = int(plate)
            row = int(row)
            col = int(col)
            if plate not in (1, 2, 3):
                return False, "Intake plate must be 1, 2, or 3."
            if row < 1 or row > 5:
                return False, "Intake row must be 1-5."
            if col < 1 or col > 3:
                return False, "Intake column must be 1-3."

            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual Dobot movement."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before manual Dobot movement."
            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if not getattr(self.robot_sequencer, "plate_manager", None):
                return False, "Plate calibration not set. Connect Dobot with calibration file."
            if self.robot_sequencer.is_busy():
                return False, "Robot is busy. Wait for current robot tasks to finish."

            with self.robot_sequencer._load_sequence_lock:
                self.set_status("Preparing", f"Dobot hover move to intake P{plate} {row},{col}")
                self.robot_sequencer.move_to_intake_hover(plate, row, col)
            self.set_status("Ready", f"Dobot at intake hover P{plate} {row},{col}")
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_set_dobot_gripper(self, enabled: bool) -> Tuple[bool, str]:
        """Admin: manually set Dobot gripper/vacuum DO."""
        try:
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual Dobot control."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before manual Dobot control."
            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if self.robot_sequencer.is_busy():
                return False, "Robot is busy. Wait for current robot tasks to finish."

            from robot_loader import set_output
            state = "on" if bool(enabled) else "off"
            with self.robot_sequencer._load_sequence_lock:
                set_output(self.robot_sequencer.dobot_client, 2, state, timeout_s=0.5, safe_to_retry=False)
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_jog_dobot(self, dx: float, dy: float, dz: float, dr: float) -> Tuple[bool, str]:
        """Admin: manual Dobot jog from tracked manual pose."""
        try:
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual Dobot control."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before manual Dobot control."
            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if self.robot_sequencer.is_busy():
                return False, "Robot is busy. Wait for current robot tasks to finish."

            with self.robot_sequencer._load_sequence_lock:
                self.robot_sequencer.jog_manual(dx=dx, dy=dy, dz=dz, dr=dr)
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_remove_stopper(self, plate: int, row: int, col: int) -> Tuple[bool, str]:
        """Admin: run stopper removal sequence from selected intake well."""
        try:
            plate = int(plate)
            row = int(row)
            col = int(col)
            if plate not in (1, 2, 3):
                return False, "Intake plate must be 1, 2, or 3."
            if row < 1 or row > 5:
                return False, "Intake row must be 1-5."
            if col < 1 or col > 3:
                return False, "Intake column must be 1-3."

            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before stopper removal."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before stopper removal."
            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if not getattr(self.robot_sequencer, "plate_manager", None):
                return False, "Plate calibration not set. Connect Dobot with calibration file."
            if self.robot_sequencer.is_busy():
                return False, "Robot is busy. Wait for current robot tasks to finish."

            with self.robot_sequencer._load_sequence_lock:
                self.set_status("Preparing", f"Removing stopper from P{plate} {row},{col}")
                self.robot_sequencer.remove_stopper_sequence(plate, row, col)
            self.set_status("Ready", f"Stopper removal complete for P{plate} {row},{col}")
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_load_line_from_intake(self, line: int, plate: int, row: int, col: int) -> Tuple[bool, str]:
        """Admin: load a specific line from a specified intake well."""
        try:
            if line not in (1, 2, 3):
                return False, "Line must be 1, 2, or 3."
            if plate not in (1, 2, 3):
                return False, "Intake plate must be 1, 2, or 3."
            if row < 1 or row > 5:
                return False, "Intake row must be 1-5."
            if col < 1 or col > 3:
                return False, "Intake column must be 1-3."

            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual loading."

            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            if not getattr(self.robot_sequencer, "plate_manager", None):
                return False, "Plate calibration not set. Connect Dobot with a calibration file."

            self._ensure_robot_loader()
            if not (hasattr(self.robot_sequencer, "robot_loader") and self.robot_sequencer.robot_loader):
                return False, "Robot loader not ready. Connect Dobot + microcontroller first."

            lipid_name = self.lipid_manager.intake_allocations.get((plate, row, col))
            if not lipid_name:
                return False, f"No lipid allocated at intake well P{plate} {row},{col}."

            line_state = self.lipid_manager.get_line_state(line)
            if line_state.get("lipid_name") and line_state.get("remaining_volume", 0) > 0:
                return False, f"Line {line} already loaded with {line_state.get('lipid_name')}."

            if not self.robot_sequencer.is_running:
                self.robot_sequencer.start()

            self.set_status("Loading", f"Line {line}: {lipid_name} from P{plate} {row},{col}")
            self.robot_sequencer.queue_load_lipid(line, plate, row, col, lipid_name)
            return True, ""
        except Exception as e:
            return False, str(e)

    def _iter_well_sequence(
        self,
        start_well: Tuple[int, int, int],
        end_well: Tuple[int, int, int],
    ) -> List[Tuple[int, int, int]]:
        start_key = (int(start_well[0]), int(start_well[1]), int(start_well[2]))
        end_key = (int(end_well[0]), int(end_well[1]), int(end_well[2]))
        wells = [
            (plate, row, col)
            for plate in range(1, 7)
            for row in range(1, 9)
            for col in range(1, 13)
        ]
        idx_map = {well: idx for idx, well in enumerate(wells)}
        if start_key not in idx_map or end_key not in idx_map:
            raise ValueError("Start/end well out of supported range.")
        i0 = idx_map[start_key]
        i1 = idx_map[end_key]
        if i0 <= i1:
            return wells[i0:i1 + 1]
        return list(reversed(wells[i1:i0 + 1]))

    def admin_run_well_to_well_test(
        self,
        start_plate: int,
        start_row: int,
        start_col: int,
        end_plate: int,
        end_row: int,
        end_col: int,
        pre_collect_wait_s: float,
        hold_time_s: float,
        perform_collection: bool,
    ) -> Tuple[bool, str]:
        """Admin: move through all wells from start to end inclusive, optionally doing collect motions."""
        try:
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            if self.admin_is_random_dobot_running():
                return False, "Stop random Dobot movement before manual movement."
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before manual movement."
            with self._admin_motion_lock:
                if self._admin_motion_thread and self._admin_motion_thread.is_alive():
                    return False, "Another admin movement routine is already running."

            start_well = (int(start_plate), int(start_row), int(start_col))
            end_well = (int(end_plate), int(end_row), int(end_col))
            path = self._iter_well_sequence(start_well, end_well)
            pre_collect_wait_s = max(0.0, float(pre_collect_wait_s))
            hold_time_s = max(0.0, float(hold_time_s))
            perform_collection = bool(perform_collection)

            def _worker():
                try:
                    self.microfluidic_ctrl.set_plate_calibration(self._plate_calibration)
                    self.set_status("Preparing", f"Well sweep: moving to start P{start_well[0]} {start_well[1]},{start_well[2]}")
                    self.microfluidic_ctrl.move_to_well(start_well, rehome=False)
                    total = len(path)
                    for idx, well in enumerate(path, start=1):
                        if idx > 1:
                            self.set_status("Preparing", f"Well sweep: moving to P{well[0]} {well[1]},{well[2]} ({idx}/{total})")
                            self.microfluidic_ctrl.move_to_well(well, rehome=False)
                        self.set_status("Preparing", f"Well sweep: at P{well[0]} {well[1]},{well[2]} ({idx}/{total})")
                        if perform_collection:
                            if pre_collect_wait_s > 0:
                                self.set_status(
                                    "Preparing",
                                    f"Well sweep: pre-collect wait {pre_collect_wait_s:.1f}s at P{well[0]} {well[1]},{well[2]}"
                                )
                                time.sleep(pre_collect_wait_s)
                            self.set_status("Preparing", f"Well sweep: collect motion at P{well[0]} {well[1]},{well[2]}")
                            self.microfluidic_ctrl.enter_calibration_mode()
                            if hold_time_s > 0:
                                time.sleep(hold_time_s)
                            self.microfluidic_ctrl.exit_calibration_mode()
                        elif hold_time_s > 0:
                            time.sleep(hold_time_s)
                    self.set_status("Ready", f"Well sweep complete ({total} wells)")
                except Exception as e:
                    self.set_status("Error", f"Well sweep failed: {e}")

            thread = threading.Thread(target=_worker, daemon=True)
            with self._admin_motion_lock:
                self._admin_motion_thread = thread
            thread.start()
            return True, f"Well sweep started for {len(path)} wells."
        except Exception as e:
            return False, str(e)

    def admin_is_random_dobot_running(self) -> bool:
        try:
            with self._random_dobot_lock:
                return bool(self._random_dobot_thread and self._random_dobot_thread.is_alive())
        except Exception:
            return False

    def admin_start_random_dobot(self) -> Tuple[bool, str]:
        """Admin: continuously move random nozzles between holding and random intake wells until stopped."""
        try:
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before random Dobot movement."
            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if not getattr(self.robot_sequencer, "plate_manager", None):
                return False, "Plate calibration not set. Connect Dobot with calibration file."
            with self._admin_motion_lock:
                if self._admin_motion_thread and self._admin_motion_thread.is_alive():
                    return False, "Another admin movement routine is already running."
            with self._random_dobot_lock:
                if self._random_dobot_thread and self._random_dobot_thread.is_alive():
                    return False, "Random Dobot movement is already running."
            if self.robot_sequencer.is_busy():
                return False, "Robot is busy. Wait for current robot tasks to finish."

            self._random_dobot_stop.clear()

            def _worker():
                try:
                    seq = self.robot_sequencer
                    def _stop_requested() -> bool:
                        return bool(self._random_dobot_stop.is_set())

                    while not self._random_dobot_stop.is_set():
                        line = random.randint(1, 3)
                        plate = random.randint(1, 3)
                        row = random.randint(1, 5)
                        col = random.randint(1, 3)
                        hold_plate, hold_row, hold_col = seq.HOLDING_POSITIONS[line]
                        self.set_status(
                            "Preparing",
                            f"Random Dobot: line {line} -> P{plate} {chr(64 + row)}{col}"
                        )
                        with seq._load_sequence_lock:
                            if _stop_requested():
                                break
                            print(f"[Admin] Random Dobot cycle: line {line} -> P{plate} {row},{col}")
                            seq._status(f"Random Dobot: line {line} pick from holding")
                            seq._pick_up(hold_plate, hold_row, hold_col)
                            if _stop_requested():
                                seq._status(f"Random Dobot: line {line} return to holding")
                                seq._place_down(hold_plate, hold_row, hold_col)
                                break
                            seq._status(f"Random Dobot: line {line} place at P{plate} {chr(64 + row)}{col}")
                            seq._place_down(plate, row, col, loading_handoff=True)
                            if _stop_requested():
                                break
                            seq._status(f"Random Dobot: line {line} pick from P{plate} {chr(64 + row)}{col}")
                            seq._pick_up(plate, row, col, loading_handoff=True)
                            if _stop_requested():
                                seq._status(f"Random Dobot: line {line} return to holding")
                                seq._place_down(hold_plate, hold_row, hold_col)
                                break
                            seq._status(f"Random Dobot: line {line} return to holding")
                            seq._place_down(hold_plate, hold_row, hold_col)
                        time.sleep(0.1)
                    self.set_status("Ready", "Random Dobot stopped")
                except Exception as e:
                    self.set_status("Error", f"Random Dobot failed: {e}")
                finally:
                    with self._random_dobot_lock:
                        self._random_dobot_thread = None
                    self._random_dobot_stop.clear()

            thread = threading.Thread(target=_worker, daemon=True)
            with self._random_dobot_lock:
                self._random_dobot_thread = thread
            thread.start()
            return True, "Random Dobot started."
        except Exception as e:
            return False, str(e)

    def admin_stop_random_dobot(self) -> Tuple[bool, str]:
        """Admin: stop the random Dobot movement loop after the current cycle completes."""
        try:
            with self._random_dobot_lock:
                thread = self._random_dobot_thread
            if not thread or not thread.is_alive():
                return False, "Random Dobot is not running."
            self._random_dobot_stop.set()
            self.set_status("Preparing", "Random Dobot: stop requested")
            return True, "Stop requested. Waiting for current cycle to finish."
        except Exception as e:
            return False, str(e)

    def admin_declare_line_loaded(self, line: int, lipid_name: str, volume_ul: float = 450.0) -> Tuple[bool, str]:
        """Admin: manually declare a line as loaded without robot actions."""
        try:
            line = int(line)
            if line not in (1, 2, 3):
                return False, "Line must be 1-3."
            lipid_name = str(lipid_name or "").strip()
            if not lipid_name:
                return False, "Select a lipid."
            vol = float(volume_ul)
            if vol < 0:
                return False, "Volume must be >= 0."
            self.lipid_manager.set_line_loaded_manual(line, lipid_name, vol)
            self.status_broker.set_line_status(line, "Loaded (manual)")
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_declare_line_empty_clean(self, line: int) -> Tuple[bool, str]:
        """Admin: manually declare a line as empty/clean without robot actions."""
        try:
            line = int(line)
            if line not in (1, 2, 3):
                return False, "Line must be 1-3."
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before declaring a line clean."
            self.lipid_manager.clear_line(line)
            self.status_broker.set_line_status(line, "Empty/Clean (manual)")
            return True, ""
        except Exception as e:
            return False, str(e)

    def admin_prepare_line_switch(self, lines: List[int]) -> Tuple[bool, str]:
        """Admin: run selected-line switch protocol for next pending experiment."""
        try:
            status = self.status_broker.get_status()
            if status.get("current_experiment") or status.get("microfluidic_state") not in ("Idle", "Paused", "Stopped"):
                return False, "Stop the current run before line-switch preparation."
            if self._queue_running:
                return False, "Queue is running. Stop queue before line-switch preparation."

            if not self.robot_sequencer.dobot_client:
                return False, "Dobot is not connected."
            if not self.is_microcontroller_connected():
                return False, "Microcontroller is not connected."
            if not getattr(self.robot_sequencer, "plate_manager", None):
                return False, "Plate calibration not set. Connect Dobot with calibration file."

            selected = sorted({int(x) for x in (lines or []) if int(x) in (1, 2, 3)})
            if not selected:
                return False, "Select at least one line (1-3)."

            queue = self.queue_manager.get_queue()
            next_exp = next((e for e in queue if e.status in ("pending", "stopped", "paused")), None)
            if not next_exp:
                return False, "No pending experiment found."

            self._ensure_robot_loader()
            if not (hasattr(self.robot_sequencer, "robot_loader") and self.robot_sequencer.robot_loader):
                return False, "Robot loader not ready. Connect Dobot + microcontroller first."

            if not self.robot_sequencer.is_running:
                self.robot_sequencer.start()

            threading.Thread(
                target=self._run_admin_prepare_line_switch,
                args=(selected, next_exp),
                daemon=True,
            ).start()
            return True, (
                f"Running switch protocol for {next_exp.name} on lines: "
                + ", ".join(f"L{l}" for l in selected)
            )
        except Exception as e:
            return False, str(e)

    def _run_admin_prepare_line_switch(self, selected_lines: List[int], next_exp) -> None:
        """Background worker: clean switched lines in parallel, then load selected lines sequentially."""
        try:
            self._last_load_failure = None
            self.status_broker.set_all_line_status("Idle")
            self.set_status("Preparing", f"Line-switch: evaluating selected lines for {next_exp.name}")

            line_targets = {}
            for line_idx in selected_lines:
                lipid_idx = line_idx - 1
                if lipid_idx >= len(next_exp.lipid_stocks):
                    self.status_broker.set_line_status(line_idx, "No target lipid in next experiment")
                    continue
                target_name = str((next_exp.lipid_stocks[lipid_idx] or {}).get("name", "")).strip()
                if not target_name:
                    self.status_broker.set_line_status(line_idx, "No target lipid in next experiment")
                    continue
                line_targets[line_idx] = target_name

            lines_to_clean = []
            lines_to_load = []
            for line_idx, target_name in line_targets.items():
                line_state = self.lipid_manager.get_line_state(line_idx)
                current_name = str(line_state.get("lipid_name") or "").strip()
                current_vol = float(line_state.get("remaining_volume", 0) or 0)
                if current_name == target_name and current_vol > 0:
                    self.status_broker.set_line_status(line_idx, "Already loaded")
                    continue
                lines_to_load.append((line_idx, target_name))
                # Conservative switch policy for admin protocol:
                # clean unless line is explicitly confirmed as already loaded with target lipid.
                lines_to_clean.append(line_idx)

            if lines_to_clean:
                self.set_status(
                    "Cleaning",
                    "Switch clean (parallel): " + ", ".join(f"L{ln}" for ln in lines_to_clean),
                )
                for line_idx in lines_to_clean:
                    self.status_broker.set_line_status(line_idx, "Cleaning")
                self.robot_sequencer.queue_clean_lines(lines_to_clean, clean_volume_ul=10.0)
                if not self.robot_sequencer.wait_until_idle(timeout_s=900.0):
                    raise RuntimeError("Switch clean timeout")
                clean_err = self.robot_sequencer.pop_last_error()
                if clean_err:
                    raise RuntimeError(f"Switch clean failed: {clean_err}")

            if not lines_to_load:
                self.set_status("Ready", "Line-switch complete: selected lines already loaded")
                return

            self.set_status(
                "Loading",
                "Switch load (sequential): " + ", ".join(f"L{ln}" for ln, _ in lines_to_load),
            )
            for line_idx, target_name in lines_to_load:
                ok = self._ensure_lipid_loaded(
                    line_idx,
                    target_name,
                    assume_cleaned=(line_idx in lines_to_clean),
                )
                if not ok:
                    fail = dict(self._last_load_failure or {})
                    reason = fail.get("message") or f"Failed to load line {line_idx}: {target_name}"
                    raise RuntimeError(reason)

            if not self.robot_sequencer.wait_until_idle(timeout_s=300.0):
                raise RuntimeError("Switch load timeout waiting for robot idle")

            self.set_status("Ready", "Line-switch complete (clean parallel, load sequential)")
        except Exception as e:
            self.set_status("Error", str(e))
            self.status_broker.set_error(str(e))

    def admin_test_line_switch_protocol(self) -> Tuple[bool, str]:
        """Backward-compatible alias to prepare selected lines for switching."""
        return self.admin_prepare_line_switch([1, 2, 3])

    def get_experiment(self, exp_id: str):
        """Get an experiment by id."""
        return self.queue_manager.get_experiment(exp_id)

    def save_queue(self) -> None:
        """Persist queue to disk."""
        self.queue_manager._save_queue_to_disk()

    def recalculate_pending_positions(self, start_plate: int, start_row: int, start_col: int) -> Tuple[bool, str]:
        """Recalculate output well positions for pending experiments."""
        try:
            queue = self.queue_manager.get_queue()
            current_row = start_row
            current_col = start_col
            current_plate = start_plate

            for exp in queue:
                if exp.status == "pending":
                    for i in range(len(exp.compositions)):
                        exp.output_wells[i] = (current_plate, current_row, current_col)
                        current_col += 1
                        if current_col > 12:
                            current_col = 1
                            current_row += 1
                            if current_row > 8:
                                current_plate += 1
                                current_row = 1

            self.queue_manager._save_queue_to_disk()
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_lipid_color(self, lipid_name: str, default: str = "#777777") -> str:
        cfg = self.config_mgr.load_config("lipid_colors", lipid_name) or {}
        return cfg.get("color", default)

    def get_composition_color(self, comp, lipid_stocks) -> str:
        lipid_colors = {}
        for lipid in lipid_stocks:
            lipid_colors[lipid["name"]] = self.get_lipid_color(lipid["name"])
        return self._blend_color(comp, lipid_stocks, lipid_colors)

    def enter_calibration_mode(self) -> Tuple[bool, str]:
        """Enter calibration mode (collect mode + Z down)."""
        try:
            if not self.microfluidic_ctrl or not self.microfluidic_ctrl.ser:
                return False, "Microcontroller is not connected."
            threading.Thread(target=self.microfluidic_ctrl.enter_calibration_mode, daemon=True).start()
            return True, ""
        except Exception as e:
            return False, str(e)

    def exit_calibration_mode(self) -> None:
        """Exit calibration mode (Z up + waste mode)."""
        try:
            if not self.microfluidic_ctrl or not self.microfluidic_ctrl.ser:
                return
            threading.Thread(target=self.microfluidic_ctrl.exit_calibration_mode, daemon=True).start()
        except Exception:
            pass

    def _ensure_robot_loader(self):
        if self.robot_sequencer.dobot_client and self.microfluidic_ctrl.ser and self.microfluidic_ctrl.calibarr:
            self.robot_sequencer.set_robot_loader(
                RobotLoader(
                    self.robot_sequencer.dobot_client,
                    self.microfluidic_ctrl.ser,
                    getattr(self.microfluidic_ctrl, "ser_secondary", None),
                    self.microfluidic_ctrl.calibarr,
                    status_callback=self._on_robot_loader_status,
                    wait_for_idle=(
                        lambda timeout_s: True
                        if self.robot_sequencer.is_robot_thread()
                        else self.robot_sequencer.wait_until_idle(timeout_s=timeout_s)
                    ),
                    stable_flush_time_s=float(self._app_config.get("stable_flush_time_s", 30.0)),
                    stable_load_time_s=float(self._app_config.get("stable_load_time_s", 6.5)),
                    load_flush_through_chip=bool(self._app_config.get("load_flush_through_chip", False)),
                    wash_cycles=max(1, int(float(self._app_config.get("wash_cycles", 1)))),
                    cleaning_flush_pressure_mbar=float(self._app_config.get("cleaning_flush_pressure_mbar", 70.0)),
                )
            )

    def _log_runtime_event(self, source: str, message: str, details: Optional[Dict] = None) -> None:
        exp_id = str(self._active_log_exp_id or "").strip()
        if not exp_id:
            return
        try:
            self.data_logger.append_runtime_event(exp_id, source, message, details=details)
        except Exception:
            pass

    def _on_robot_sequencer_status(self, msg: str) -> None:
        text = str(msg or "")
        self.status_broker.set_ui_status(text)
        self._log_runtime_event("robot_sequencer", text)

    def _on_robot_loader_status(self, msg: str) -> None:
        text = str(msg or "")
        self._log_runtime_event("robot_loader", text)
        ltxt = text.lower()
        if any(k in ltxt for k in ("loading", "apply pressure 45", "fluid stable", "set valves for loading")):
            state = "Loading"
        elif any(k in ltxt for k in ("wash", "air flush", "prime ethanol", "clean")):
            state = "Cleaning"
        else:
            state = "Preparing"
        self.set_status(state, text)

    def delete_lipid_config(self, name):
        self.config_mgr.delete_config("lipids", name)
        return True

    def move_experiment_up(self, exp_id: str) -> bool:
        return self.queue_manager.move_experiment(exp_id, -1)

    def move_experiment_down(self, exp_id: str) -> bool:
        return self.queue_manager.move_experiment(exp_id, 1)
