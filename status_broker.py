import threading
import json
from typing import Dict, Any, Optional, List
from collections import defaultdict

class StatusBroker:
    """Thread-safe status sync between control loop and GUI."""
    
    def __init__(self):
        self._lock = threading.RLock()
        self.status = {
            "connections": {"microfluidics": False, "dobot": False, "microcontroller": False, "extra_pressure": False},
            "current_experiment": None,
            "current_composition_idx": 0,
            "robot_position": None,
            "microfluidic_state": "Idle",  # Idle, Reaching equilibrium, Equilibrated, Flushing, Collecting
            "ui_status": "",
            "run_detail": "",
            "collected_volume": 0.0,
            "target_volume": 0.0,
            "flow_data": {
                "time": [],
                "flows": [[], [], [], []],
                "pressures_set": [[], [], [], []],
                "pressures_act": [[], [], [], []],
                "extra_flow": [],
                "extra_pressure_set": [],
                "extra_pressure_act": [],
                "collection_markers": [],
            },
            "live_flows": {"ch1": None, "ch2": None, "ch3": None, "ch4": None, "extra": None, "extra_enabled": False},
            "plot_ranges": {"flow_ylim": None, "pressure_ylim": None},
            "current_well": (1, 1),
            "start_well": (1, 1, 1),
            "paused": False,
            "current_error": None,
            "recovery_prompt": None,
            "line_status": {"1": "Idle", "2": "Idle", "3": "Idle"},
        }

    def set_connection_state(self, device: str, connected: bool) -> None:
        """Update connection state."""
        with self._lock:
            self.status["connections"][device] = connected

    def set_microfluidic_state(self, state: str) -> None:
        """Update microfluidic state (Idle, Reaching equilibrium, etc)."""
        with self._lock:
            self.status["microfluidic_state"] = state

    def set_plot_ranges(self, flow_ylim: Optional[List[float]], pressure_ylim: Optional[List[float]]) -> None:
        """Set plot y-limits for flow and pressure plots."""
        with self._lock:
            self.status["plot_ranges"]["flow_ylim"] = flow_ylim
            self.status["plot_ranges"]["pressure_ylim"] = pressure_ylim

    def set_ui_status(self, text: str) -> None:
        """Update UI status text (used for non-microfluidic operations)."""
        with self._lock:
            self.status["ui_status"] = text

    def set_run_detail(self, text: str) -> None:
        """Update run detail text shown in the live data section."""
        with self._lock:
            self.status["run_detail"] = text

    def update_flow_data(
        self,
        time_point: float,
        flows: List[float],
        p_set: List[float],
        p_act: List[float],
        extra_flow: Optional[float] = None,
        extra_p_set: Optional[float] = None,
        extra_p_act: Optional[float] = None,
    ) -> None:
        """Update live flow plot data."""
        with self._lock:
            self.status["flow_data"]["time"].append(time_point)
            for i, f in enumerate(flows):
                self.status["flow_data"]["flows"][i].append(f)
            for i, p in enumerate(p_set):
                self.status["flow_data"]["pressures_set"][i].append(p)
            for i, p in enumerate(p_act):
                self.status["flow_data"]["pressures_act"][i].append(p)
            self.status["flow_data"].setdefault("extra_flow", []).append(extra_flow)
            self.status["flow_data"].setdefault("extra_pressure_set", []).append(extra_p_set)
            self.status["flow_data"].setdefault("extra_pressure_act", []).append(extra_p_act)

    def set_live_flows(
        self,
        ch_flows: List[Optional[float]],
        extra_flow: Optional[float],
        extra_enabled: bool,
    ) -> None:
        """Set latest instantaneous flow readings for compact GUI display."""
        with self._lock:
            self.status["live_flows"] = {
                "ch1": ch_flows[0] if len(ch_flows) > 0 else None,
                "ch2": ch_flows[1] if len(ch_flows) > 1 else None,
                "ch3": ch_flows[2] if len(ch_flows) > 2 else None,
                "ch4": ch_flows[3] if len(ch_flows) > 3 else None,
                "extra": extra_flow,
                "extra_enabled": bool(extra_enabled),
            }

    def add_collection_marker(self, time_point: float) -> None:
        """Add a collection start marker for plotting."""
        with self._lock:
            self.status["flow_data"]["collection_markers"].append(time_point)

    def set_collection_progress(self, collected: float, target: float) -> None:
        """Update collection volume progress."""
        with self._lock:
            self.status["collected_volume"] = collected
            self.status["target_volume"] = target

    def set_current_experiment(self, exp_id: Optional[str], composition_idx: int = 0) -> None:
        """Set current running experiment and composition index."""
        with self._lock:
            self.status["current_experiment"] = exp_id
            self.status["current_composition_idx"] = composition_idx
            if exp_id is None:
                self.status["run_detail"] = ""

    def set_robot_position(self, plate: int, row: int, col: int) -> None:
        """Update robot position."""
        with self._lock:
            self.status["robot_position"] = (plate, row, col)

    def set_current_well(self, row: int, col: int, plate: Optional[int] = None) -> None:
        """Update current well position during experiment."""
        with self._lock:
            if plate is None:
                current = self.status.get("current_well")
                if isinstance(current, (list, tuple)) and len(current) == 3:
                    self.status["current_well"] = (current[0], row, col)
                else:
                    self.status["current_well"] = (row, col)
            else:
                self.status["current_well"] = (plate, row, col)

    def set_start_well(self, plate: int, row: int, col: int) -> None:
        """Set the starting well for the next experiment."""
        with self._lock:
            self.status["start_well"] = (plate, row, col)

    def get_start_well(self) -> tuple:
        """Get the configured start well."""
        with self._lock:
            return self.status.get("start_well", (1, 1, 1))

    def set_paused(self, paused: bool) -> None:
        """Set pause state."""
        with self._lock:
            self.status["paused"] = paused

    def set_error(self, error: Optional[str]) -> None:
        """Set error message."""
        with self._lock:
            self.status["current_error"] = error

    def set_recovery_prompt(self, prompt: Optional[Dict[str, Any]]) -> None:
        """Set or clear recoverable-action prompt payload for GUI."""
        with self._lock:
            self.status["recovery_prompt"] = prompt

    def set_line_status(self, line: int, text: str) -> None:
        """Set status text for a specific line (1..3)."""
        key = str(int(line))
        if key not in ("1", "2", "3"):
            return
        with self._lock:
            self.status["line_status"][key] = str(text)

    def set_all_line_status(self, text: str) -> None:
        """Set same status text for all lines."""
        with self._lock:
            for key in ("1", "2", "3"):
                self.status["line_status"][key] = str(text)

    def get_status(self) -> Dict[str, Any]:
        """Get full status snapshot."""
        with self._lock:
            return json.loads(json.dumps(self.status))  # deep copy

    def clear_flow_data(self) -> None:
        """Clear accumulated flow data (at start of new experiment)."""
        with self._lock:
            self.status["flow_data"] = {
                "time": [],
                "flows": [[], [], [], []],
                "pressures_set": [[], [], [], []],
                "pressures_act": [[], [], [], []],
                "extra_flow": [],
                "extra_pressure_set": [],
                "extra_pressure_act": [],
                "collection_markers": [],
            }
            self.status["collected_volume"] = 0.0
            self.status["target_volume"] = 0.0
            self.status["plot_ranges"] = {"flow_ylim": None, "pressure_ylim": None}
            self.status["run_detail"] = ""
            self.status["live_flows"] = {"ch1": None, "ch2": None, "ch3": None, "ch4": None, "extra": None, "extra_enabled": False}
