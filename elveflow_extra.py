from ctypes import byref, c_double, c_int32
from pathlib import Path
from typing import Optional
import ctypes as ct
import re

import numpy as np

from Elveflow64 import (
    OB1_Initialization,
    OB1_Add_Sens,
    OB1_Set_Press,
    OB1_Get_Press,
    OB1_Get_Sens_Data,
    OB1_Calib,
    Elveflow_Calibration_Default,
)


class ElveflowExtraPump:
    """One-channel Elveflow OB1/Mk4 adapter for the extra RNA pressure controller."""

    def __init__(self) -> None:
        self.connected: bool = False
        self.last_error: str = ""
        self.pressure_channel: int = 1
        self.sensor_channel: int = 1
        self.pressure_min: float = 0.0
        self.pressure_max: float = 2000.0
        self.instr_id = c_int32()
        self.calibarr = None
        self.device_name: str = "Mk4_Extra"
        self.com_port: str = "COM6"
        self._calib_storage = None
        self._calib_array = None

    def _normalise_com_targets(self, com_port: str) -> list[str]:
        txt = str(com_port or "").strip() or "COM6"
        targets = [txt]
        m = re.fullmatch(r"COM(\d+)", txt.upper())
        if m:
            targets.append(f"ASRL{m.group(1)}::INSTR")
        return list(dict.fromkeys(targets))

    def _calibration_path(self) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.device_name or "Mk4_Extra")).strip("_")
        if not safe_name:
            safe_name = "Mk4_Extra"
        return Path.cwd() / f"calib_{safe_name}.npy"

    def _load_calibration(self, path: Path) -> None:
        print(f"[ElveflowExtraPump] Loading calibration file: {path}", flush=True)
        array = np.load(str(path), allow_pickle=True)
        if array.size < 1000:
            raise RuntimeError(f"Calibration file has {array.size} values, expected 1000.")
        array = np.asarray(array, dtype=np.float64).reshape(-1)[:1000].copy()
        self._calib_array = array
        self.calibarr = array.ctypes.data_as(ct.POINTER(ct.c_double * 1000))
        print(f"[ElveflowExtraPump] Loaded calibration from {path}", flush=True)

    def _default_calibration(self) -> None:
        print("[ElveflowExtraPump] Taking default Elveflow calibration", flush=True)
        calib = (c_double * 1000)()
        err = Elveflow_Calibration_Default(byref(calib), 1000)
        print(f"[ElveflowExtraPump] Elveflow_Calibration_Default returned {err}", flush=True)
        if abs(int(err)) != 0:
            raise RuntimeError(f"Default calibration error: {err}")
        self._calib_storage = calib
        self.calibarr = byref(calib)
        print("[ElveflowExtraPump] Default calibration taken", flush=True)

    def _new_calibration(self, path: Path) -> None:
        print("[ElveflowExtraPump] Starting new Elveflow OB1 calibration", flush=True)
        print(f"[ElveflowExtraPump] Calibration will be saved to: {path}", flush=True)
        calib = (c_double * 1000)()
        err = OB1_Calib(self.instr_id.value, calib, 1000)
        print(f"[ElveflowExtraPump] OB1_Calib returned {err}", flush=True)
        if err is not None and abs(int(err)) != 0:
            raise RuntimeError(f"New calibration error: {err}")
        array = np.ctypeslib.as_array(calib).astype(np.float64, copy=True)
        np.save(str(path), array)
        self._calib_array = array
        self.calibarr = array.ctypes.data_as(ct.POINTER(ct.c_double * 1000))
        print(f"[ElveflowExtraPump] New calibration saved to {path}", flush=True)

    def _configure_calibration(self, calibration: str) -> None:
        mode = str(calibration or "load").strip().lower()
        if mode not in ("load", "default", "new"):
            mode = "load"
        path = self._calibration_path()
        print(
            f"[ElveflowExtraPump] Calibration mode={mode}, file={path}",
            flush=True,
        )
        if mode == "load":
            if not path.exists():
                raise RuntimeError(f"Extra calibration file not found: {path}")
            self._load_calibration(path)
        elif mode == "new":
            self._new_calibration(path)
        else:
            self._default_calibration()

    def connect(
        self,
        com_port: str = "",
        device_name: str = "Mk4_Extra",
        calibration: str = "load",
    ) -> tuple[bool, str]:
        self.last_error = ""
        self.device_name = str(device_name or "Mk4_Extra")
        self.com_port = str(com_port or "COM6")
        targets = self._normalise_com_targets(self.com_port)
        print(
            f"[ElveflowExtraPump] Connecting device={self.device_name}, requested_port={self.com_port}, "
            f"targets={targets}, calibration={calibration}",
            flush=True,
        )
        last_err = ""
        for target in targets:
            try:
                print(f"[ElveflowExtraPump] OB1_Initialization target={target}", flush=True)
                err = OB1_Initialization(target.encode("ascii"), 2, 2, 2, 2, byref(self.instr_id))
                print(
                    f"[ElveflowExtraPump] OB1_Initialization returned {err}, instrument_id={self.instr_id.value}",
                    flush=True,
                )
                if abs(int(err)) == 0:
                    self.connected = True
                    break
                last_err = f"OB1_Initialization({target}) returned {err}"
            except Exception as e:
                last_err = f"OB1_Initialization({target}) failed: {e}"
        else:
            self.connected = False
            self.last_error = last_err or "Extra Elveflow controller not found"
            print(f"[ElveflowExtraPump] Connection failed: {self.last_error}", flush=True)
            return False, self.last_error

        try:
            self._configure_calibration(calibration)
            err_s = OB1_Add_Sens(self.instr_id, 1, 2, 1, 0, 7, 0)
            print(f"[ElveflowExtraPump] OB1_Add_Sens(channel=1) returned {err_s}", flush=True)
            if abs(int(err_s)) != 0:
                print(f"[ElveflowExtraPump] Warning: sensor init returned {err_s}", flush=True)
            ok, err_zero = self.set_pressure(0.0)
            print(f"[ElveflowExtraPump] Initial set_pressure(0.0) ok={ok}, err={err_zero}", flush=True)
            if not ok:
                self.connected = False
                self.last_error = f"Extra channel alarm on connect: {err_zero}"
                print(f"[ElveflowExtraPump] Connection failed: {self.last_error}", flush=True)
                return False, self.last_error
            print(f"[ElveflowExtraPump] {self.device_name} connected on {self.com_port} as channel 1", flush=True)
            return True, ""
        except Exception as e:
            self.connected = False
            self.last_error = f"Extra Elveflow init failed: {e}"
            print(f"[ElveflowExtraPump] Connection failed: {self.last_error}", flush=True)
            return False, self.last_error

    def disconnect(self) -> None:
        try:
            self.set_pressure(0.0)
        except Exception:
            pass
        self.connected = False

    def set_pressure(self, pressure_mbar: float) -> tuple[bool, str]:
        if not self.connected or self.calibarr is None:
            return False, "Extra Elveflow controller not connected"
        p_cmd = float(np.clip(float(pressure_mbar), self.pressure_min, self.pressure_max))
        try:
            err = OB1_Set_Press(
                self.instr_id.value,
                c_int32(int(self.pressure_channel)),
                c_double(p_cmd),
                self.calibarr,
                1000,
            )
            if abs(int(err)) != 0:
                return False, f"set_pressure returned: {err}"
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_pressure(self) -> tuple[Optional[float], str]:
        if not self.connected or self.calibarr is None:
            return None, "Extra Elveflow controller not connected"
        val = c_double()
        try:
            err = OB1_Get_Press(
                self.instr_id.value,
                c_int32(int(self.pressure_channel)),
                1,
                self.calibarr,
                byref(val),
                1000,
            )
            if abs(int(err)) != 0:
                return None, str(err)
            return float(val.value), ""
        except Exception as e:
            return None, str(e)

    def get_flow(self) -> tuple[Optional[float], str]:
        if not self.connected:
            return None, "Extra Elveflow controller not connected"
        val = c_double()
        try:
            err = OB1_Get_Sens_Data(self.instr_id.value, c_int32(int(self.sensor_channel)), 1, byref(val))
            if abs(int(err)) != 0:
                return None, str(err)
            return float(val.value), ""
        except Exception as e:
            return None, str(e)
