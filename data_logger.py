import json
import os
import queue
import threading
import csv
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

import numpy as np

import records

class DataLogger:
    """Log intermediate and final experiment data."""
    
    def __init__(self, base_dir: str = "./temp_state"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        os.makedirs(f"{base_dir}/experiments", exist_ok=True)
        self._lock = threading.RLock()
        self._ctx_by_exp: Dict[str, Dict[str, Any]] = {}
        self._run_ctx: Dict[str, Any] = {}
        self._tasks: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._worker = threading.Thread(target=self._writer_loop, daemon=True)
        self._worker.start()

    def init_experiment_log(self, exp_id: str, exp_params: Dict[str, Any] = None) -> None:
        """Create new experiment temp file."""
        exp_file = f"{self.base_dir}/experiments/{exp_id}.json"
        existing_runtime_events: List[Dict[str, Any]] = []
        if os.path.exists(exp_file):
            try:
                with open(exp_file, "r") as f:
                    existing = json.load(f) or {}
                existing_runtime_events = list(existing.get("runtime_events") or [])
            except Exception:
                existing_runtime_events = []
        data = {
            "exp_id": exp_id,
            "start_time": datetime.now().isoformat(),
            "exp_params": exp_params or {},
            "flow_readings": [],
            "collections": [],
            "runtime_events": existing_runtime_events,
        }
        with open(exp_file, "w") as f:
            json.dump(data, f, default=self._json_default)

        params = dict(exp_params or {})
        # Internal priming runs should not consume user experiment record IDs / folders.
        if str(exp_id) == "prime_flush" or bool(params.get("_internal_priming", False)):
            return
        try:
            rec_id = records.get_next_id()
            exp_name = str(params.get("exp_name") or exp_id)
            run_root = str((self._run_ctx or {}).get("run_dir") or "./FlowData")
            expname, fpath = records.create_experiment_folder(str(rec_id), exp_name, root_dir=run_root)
            with self._lock:
                self._ctx_by_exp[exp_id] = {
                    "expname": expname,
                    "fpath": fpath,
                    "record_id": int(rec_id),
                    "exp_params": params,
                    "comp_flow": {},
                }
            self._append_run_manifest(exp_id, int(rec_id), params, expname)
            self._tasks.put({"type": "save_params", "exp_id": exp_id})
        except Exception as e:
            print(f"[DataLogger] Warning: could not initialize records context for {exp_id}: {e}")

    def begin_run(self, queue_snapshot: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Start a run-level folder so sequential experiments share one parent folder."""
        with self._lock:
            if self._run_ctx.get("active"):
                return dict(self._run_ctx)
            run_id = int(records.get_next_run_id())
            ts = datetime.now()
            run_name = f"run_{run_id}-{ts.strftime('%y%m%d-%H%M%S')}"
            run_dir = os.path.join("./FlowData", run_name)
            os.makedirs(run_dir, exist_ok=True)
            os.makedirs(os.path.join(run_dir, "Flowplots"), exist_ok=True)
            manifest_path = os.path.join(run_dir, "run_compositions.csv")
            run_flow_csv = os.path.join(run_dir, "run_full_timeseries.csv")
            self._run_ctx = {
                "active": True,
                "run_id": run_id,
                "run_name": run_name,
                "run_dir": run_dir,
                "started_at": ts.isoformat(),
                "started_epoch": ts.timestamp(),
                "manifest_path": manifest_path,
                "run_flow_csv": run_flow_csv,
                "manifest_written_exps": set(),
                "queue_snapshot": list(queue_snapshot or []),
            }
            self._init_run_timeseries_csv(run_flow_csv)
            meta = {
                "run_id": run_id,
                "run_name": run_name,
                "started_at": ts.isoformat(),
                "run_flow_csv": "run_full_timeseries.csv",
                "queue_snapshot": queue_snapshot or [],
            }
            with open(os.path.join(run_dir, "run_metadata.json"), "w") as f:
                json.dump(meta, f, indent=2, default=self._json_default)
            print(f"[DataLogger] Run folder started: {run_dir}")
            return dict(self._run_ctx)

    def end_run(self) -> None:
        """Mark current run folder inactive (does not delete anything)."""
        with self._lock:
            if not self._run_ctx.get("active"):
                return
            run_dir = self._run_ctx.get("run_dir")
            run_id = self._run_ctx.get("run_id")
            run_flow_csv = self._run_ctx.get("run_flow_csv")
            started_at = self._run_ctx.get("started_at")
            run_name = self._run_ctx.get("run_name")
            queue_snapshot = list(self._run_ctx.get("queue_snapshot") or [])
            try:
                ended_at = datetime.now().isoformat()
                plot_outputs = self._generate_run_timeseries_plots(run_dir, run_flow_csv)
                summary = {
                    "run_id": run_id,
                    "ended_at": ended_at,
                    "run_flow_csv": "run_full_timeseries.csv" if run_flow_csv else None,
                    "run_plots": plot_outputs,
                }
                with open(os.path.join(run_dir, "run_end.json"), "w") as f:
                    json.dump(summary, f, indent=2, default=self._json_default)
                self._update_daily_manifest(
                    run_id=run_id,
                    run_name=str(run_name or ""),
                    run_dir=str(run_dir or ""),
                    started_at=str(started_at or ""),
                    ended_at=ended_at,
                    run_flow_csv="run_full_timeseries.csv" if run_flow_csv else "",
                    plot_outputs=plot_outputs,
                )
                self._update_runs_index(
                    run_id=run_id,
                    run_name=str(run_name or ""),
                    run_dir=str(run_dir or ""),
                    started_at=str(started_at or ""),
                    ended_at=ended_at,
                    run_flow_csv="run_full_timeseries.csv" if run_flow_csv else "",
                    plot_outputs=plot_outputs,
                    queue_count=len(queue_snapshot),
                )
            except Exception:
                pass
            print(f"[DataLogger] Run folder closed: {run_dir}")
            self._run_ctx = {}

    def append_flow_reading(
        self,
        exp_id: str,
        timestamp: float,
        flows: List[float],
        pressures_act: List[float],
        pressures_set: Optional[List[float]] = None,
        extra_flow: Optional[float] = None,
        extra_pressure_act: Optional[float] = None,
        extra_pressure_set: Optional[float] = None,
    ) -> None:
        """Append a flow reading to the run-level timeseries CSV."""
        try:
            self._append_run_timeseries_row(
                exp_id=exp_id,
                timestamp=timestamp,
                flows=flows,
                pressures_act=pressures_act,
                pressures_set=pressures_set or [0.0, 0.0, 0.0, 0.0],
                extra_flow=extra_flow,
                extra_pressure_act=extra_pressure_act,
                extra_pressure_set=extra_pressure_set,
            )
        except Exception as e:
            print(f"[DataLogger] Warning: Could not append run timeseries row: {e}")

    def append_collection_event(self, exp_id: str, composition_idx: int, volume_collected: float) -> None:
        """Log collection event."""
        exp_file = f"{self.base_dir}/experiments/{exp_id}.json"
        try:
            with open(exp_file, "r") as f:
                data = json.load(f)
            data["collections"].append({
                "composition_idx": composition_idx,
                "volume_collected": volume_collected,
                "timestamp": datetime.now().isoformat()
            })
            with open(exp_file, "w") as f:
                json.dump(data, f, default=self._json_default)
        except Exception as e:
            print(f"Warning: Could not log collection event: {e}")
        self._tasks.put({
            "type": "save_comp_record",
            "exp_id": exp_id,
            "composition_idx": int(composition_idx),
            "volume_collected": float(volume_collected),
        })

    def append_runtime_event(
        self,
        exp_id: str,
        source: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append runtime event (robot clean/load state machine, debug traces) to experiment log."""
        if not exp_id:
            return
        exp_file = f"{self.base_dir}/experiments/{exp_id}.json"
        event = {
            "timestamp": datetime.now().isoformat(),
            "source": str(source or "runtime"),
            "message": str(message or ""),
            "details": dict(details or {}),
        }
        try:
            with self._lock:
                data: Dict[str, Any]
                if os.path.exists(exp_file):
                    with open(exp_file, "r") as f:
                        data = json.load(f)
                else:
                    data = {
                        "exp_id": exp_id,
                        "start_time": datetime.now().isoformat(),
                        "exp_params": {},
                        "flow_readings": [],
                        "collections": [],
                        "runtime_events": [],
                    }
                data.setdefault("runtime_events", [])
                data["runtime_events"].append(event)
                with open(exp_file, "w") as f:
                    json.dump(data, f, default=self._json_default)
        except Exception as e:
            print(f"[DataLogger] Warning: Could not log runtime event for {exp_id}: {e}")

    def store_composition_flow_data(
        self,
        comp_idx: int,
        time_data: List[float],
        flows: List[List[float]],
        pressures_set: List[List[float]],
        pressures_act: List[List[float]],
        set_FR: List[float],
        flowstart: int,
        exp_id: Optional[str] = None,
        extra_flow: Optional[List[Optional[float]]] = None,
        extra_pressure_set: Optional[List[Optional[float]]] = None,
        extra_pressure_act: Optional[List[Optional[float]]] = None,
    ) -> None:
        """Store per-composition flow data in memory for async record writing."""
        # Best-effort lookup if caller doesn't provide exp_id.
        if exp_id is None:
            with self._lock:
                if self._ctx_by_exp:
                    exp_id = next(reversed(self._ctx_by_exp))
        if not exp_id:
            return
        with self._lock:
            ctx = self._ctx_by_exp.get(exp_id)
            if not ctx:
                return
            ctx["comp_flow"][int(comp_idx)] = {
                "time_data": list(time_data),
                "flows": [list(ch) for ch in flows],
                "pressures_set": [list(ch) for ch in pressures_set],
                "pressures_act": [list(ch) for ch in pressures_act],
                "extra_flow": list(extra_flow or []),
                "extra_pressure_set": list(extra_pressure_set or []),
                "extra_pressure_act": list(extra_pressure_act or []),
                "set_FR": list(set_FR),
                "flowstart": int(flowstart),
            }

    def finalize_experiment(self, exp_id: str, final_data: Dict[str, Any]) -> None:
        """Move temp file to final logs directory and add summary."""
        exp_file = f"{self.base_dir}/experiments/{exp_id}.json"
        final_dir = f"./logs/{datetime.now().strftime('%Y%m%d')}"
        os.makedirs(final_dir, exist_ok=True)
        
        try:
            with open(exp_file, "r") as f:
                temp_data = json.load(f)
            temp_data.update(final_data)
            temp_data["end_time"] = datetime.now().isoformat()
            
            with open(f"{final_dir}/{exp_id}.json", "w") as f:
                json.dump(temp_data, f, indent=2, default=self._json_default)
            
            os.remove(exp_file)
        except Exception as e:
            print(f"Warning: Could not finalize experiment: {e}")

    def get_record_number(self, exp_id: str) -> Optional[int]:
        """Return records folder numeric prefix (e.g. 145 from '145-YYMMDD-name') if known."""
        with self._lock:
            ctx = self._ctx_by_exp.get(exp_id) or {}
            expname = str(ctx.get("expname") or "")
        if not expname:
            return None
        try:
            prefix = expname.split("-", 1)[0]
            return int(prefix)
        except Exception:
            return None

    def _append_run_manifest(self, exp_id: str, rec_id: int, params: Dict[str, Any], expname: str) -> None:
        """Append all planned compositions for an experiment into the current run manifest CSV."""
        with self._lock:
            run_ctx = dict(self._run_ctx or {})
            if not run_ctx.get("active"):
                return
            written = self._run_ctx.setdefault("manifest_written_exps", set())
            if exp_id in written:
                return
            manifest_path = run_ctx.get("manifest_path")
            run_id = run_ctx.get("run_id")
            written.add(exp_id)

        lipid_stocks = list(params.get("lipid_stocks") or [])
        output_wells = list(params.get("output_wells") or [])
        compositions = list(params.get("compositions") or [])
        flow_rates = list(params.get("flow_rates") or [])
        flow_rates_exec = list(params.get("flow_rates_exec") or [])
        orig_lipid_stocks = list(params.get("original_lipid_stocks") or [])
        remap_perm = list(params.get("runtime_slot_perm_new_to_old") or [])
        slot_to_line = list(params.get("runtime_slot_to_line") or [])
        line3_rna_mode = bool(params.get("line3_constant_flow_enabled", False))
        target_volume_ul = float(params.get("volume", 0.0) or 0.0)
        buffer_cfg = params.get("buffer") or {}
        buffer_name = str(buffer_cfg.get("name") or "Buffer")

        def _lipid_code(lipid: Any) -> str:
            if not isinstance(lipid, dict):
                return ""
            return str(lipid.get("lipid_code") or lipid.get("code") or lipid.get("name") or "")

        def _dump(value: Any) -> str:
            return json.dumps(value, default=self._json_default)

        # Prefer original slot order when no runtime remap was applied.
        use_lipids = lipid_stocks
        if remap_perm and remap_perm == list(range(len(remap_perm))) and orig_lipid_stocks:
            use_lipids = orig_lipid_stocks

        # Per-experiment reagent consumption totals (uL), estimated from set flow splits.
        # Uses planned per-composition target volume for each composition row.
        channel_totals = [0.0, 0.0, 0.0, 0.0]  # [buffer,ch2,ch3,ch4]
        for i in range(len(compositions)):
            fr_exec = flow_rates_exec[i] if i < len(flow_rates_exec) else []
            frv = [0.0, 0.0, 0.0, 0.0]
            for j in range(min(4, len(fr_exec))):
                try:
                    frv[j] = max(0.0, float(fr_exec[j]))
                except Exception:
                    frv[j] = 0.0
            total_fr = float(np.sum(frv))
            if total_fr <= 0 or target_volume_ul <= 0:
                continue
            for j in range(4):
                channel_totals[j] += target_volume_ul * (frv[j] / total_fr)

        line_name_by_channel = {2: "", 3: "", 4: ""}
        for line in (1, 2, 3):
            ch = line + 1
            if line == 3 and line3_rna_mode:
                line_name_by_channel[ch] = "RNA Buffer"
                continue
            if line in slot_to_line:
                try:
                    slot_idx = int(slot_to_line.index(line))
                    if 0 <= slot_idx < len(use_lipids):
                        line_name_by_channel[ch] = str((use_lipids[slot_idx] or {}).get("name") or "")
                except Exception:
                    pass

        reagent_totals: Dict[str, float] = {}
        reagent_totals[buffer_name] = reagent_totals.get(buffer_name, 0.0) + float(channel_totals[0])
        for ch in (2, 3, 4):
            nm = str(line_name_by_channel.get(ch) or "").strip()
            if not nm:
                continue
            reagent_totals[nm] = reagent_totals.get(nm, 0.0) + float(channel_totals[ch - 1])

        rows = []
        for i, comp in enumerate(compositions):
            well = output_wells[i] if i < len(output_wells) else [None, None, None]
            fr = flow_rates[i] if i < len(flow_rates) else []
            fr_exec = flow_rates_exec[i] if i < len(flow_rates_exec) else []
            rows.append({
                "run_id": run_id,
                "exp_record_id": rec_id,
                "exp_id": str(exp_id),
                "exp_folder": str(expname),
                "exp_name": str(params.get("exp_name") or ""),
                "comp_index_1based": i + 1,
                "well_plate": well[0] if isinstance(well, (list, tuple)) and len(well) > 0 else None,
                "well_row": well[1] if isinstance(well, (list, tuple)) and len(well) > 1 else None,
                "well_col": well[2] if isinstance(well, (list, tuple)) and len(well) > 2 else None,
                "lipid_codes": _dump([_lipid_code(l) for l in use_lipids]),
                "lipid_names": _dump([str((l or {}).get("name", "")) for l in use_lipids]),
                "slot_to_line": _dump(slot_to_line),
                "ratios": _dump(list(comp)),
                "flow_rates": _dump(list(fr)),              # logical slot order
                "flow_rates_exec": _dump(list(fr_exec)),    # physical channel order [buf,ch2,ch3,ch4]
                "line1_lipid_code": _lipid_code(use_lipids[slot_to_line.index(1)]) if 1 in slot_to_line and slot_to_line.index(1) < len(use_lipids) else "",
                "line2_lipid_code": _lipid_code(use_lipids[slot_to_line.index(2)]) if 2 in slot_to_line and slot_to_line.index(2) < len(use_lipids) else "",
                "line3_lipid_code": _lipid_code(use_lipids[slot_to_line.index(3)]) if 3 in slot_to_line and slot_to_line.index(3) < len(use_lipids) else "",
                "total_buffer_consumption_ul": round(float(channel_totals[0]), 3),
                "total_line1_consumption_ul": round(float(channel_totals[1]), 3),
                "total_line2_consumption_ul": round(float(channel_totals[2]), 3),
                "total_line3_consumption_ul": round(float(channel_totals[3]), 3),
                "total_rna_buffer_consumption_ul": round(float(channel_totals[3] if line3_rna_mode else 0.0), 3),
                "reagent_consumption_ul_json": _dump({k: round(float(v), 3) for k, v in reagent_totals.items()}),
            })

        if not rows or not manifest_path:
            return
        import pandas as pd
        df = pd.DataFrame(rows)
        header = not os.path.exists(manifest_path)
        df.to_csv(manifest_path, mode="a", header=header, index=False)
        print(f"[DataLogger] Run manifest updated: {manifest_path} (+{len(rows)} rows)")

    def cleanup_temp(self) -> None:
        """Remove all temp files."""
        import shutil
        try:
            shutil.rmtree(f"{self.base_dir}/experiments")
            os.makedirs(f"{self.base_dir}/experiments", exist_ok=True)
        except Exception as e:
            print(f"Warning: Could not cleanup temp files: {e}")

    def shutdown(self) -> None:
        """Stop background writer thread."""
        self._tasks.put(None)
        try:
            self._worker.join(timeout=2.0)
        except Exception:
            pass
        with self._lock:
            self._ctx_by_exp.clear()

    def _writer_loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                break
            try:
                t = task.get("type")
                if t == "save_params":
                    self._write_parameter_record(task["exp_id"])
                elif t == "save_comp_record":
                    self._write_composition_record(
                        task["exp_id"],
                        int(task["composition_idx"]),
                        float(task["volume_collected"]),
                    )
            except Exception as e:
                print(f"[DataLogger] Writer task failed: {e}")

    def _write_parameter_record(self, exp_id: str) -> None:
        with self._lock:
            ctx = self._ctx_by_exp.get(exp_id)
            if not ctx:
                return
            p = dict(ctx.get("exp_params") or {})
            expname = ctx["expname"]
            fpath = ctx["fpath"]

        buffer_cfg = p.get("buffer") or {}
        records.save_parameter_record(
            expname=expname,
            fpath=fpath,
            exp_id=exp_id,
            exp_name=str(p.get("exp_name", exp_id)),
            details=str(p.get("details", "")),
            buffer_name=str(buffer_cfg.get("name", "Buffer")),
            buffer_params=buffer_cfg,
            buffer_notes=str(p.get("buffer_notes", "")),
            lipid_stocks=list(p.get("lipid_stocks") or []),
            lipid_notes=list(p.get("lipid_notes") or []),
            compositions=list(p.get("compositions") or []),
            flow_rates=list(p.get("flow_rates") or []),
            inst_name=str(p.get("inst_name", "")),
            active_chans=list(p.get("active_channels") or [1, 2, 3, 4]),
            sensorcorr=list(p.get("sensorcorr") or []),
            volume=float(p.get("volume", 0.0)),
            repeats=int(p.get("repeats", 1)),
            period=float(p.get("period", 0.5)),
            K_p=p.get("K_p", [0.5, 500, 500, 500]),
            K_i=float(p.get("K_i", 0.001)),
            p_incr=list(p.get("p_incr") or [-100, 100]),
            p_range=list(p.get("p_range") or [0, 2000]),
            max_eq_t=float(p.get("eq_max_t", 180)),
            eq_abs_error=list(p.get("maxfrerror") or [100, 0.2]),
            tfr=float(p.get("tfr", 0.0)),
            frr=float(p.get("frr", 0.0)),
            screen_space_mode=str(p.get("screen_space_mode", "Manual")),
            screen_space_params=dict(p.get("screen_space_params") or {}),
            app_config=dict(p.get("app_config") or {}),
            flow_rates_exec=list(p.get("flow_rates_exec") or []),
            runtime_slot_to_line=list(p.get("runtime_slot_to_line") or []),
        )

    def _write_composition_record(self, exp_id: str, comp_idx: int, volume_collected: float) -> None:
        with self._lock:
            ctx = self._ctx_by_exp.get(exp_id)
            if not ctx:
                return
            p = dict(ctx.get("exp_params") or {})
            comp_flow = dict((ctx.get("comp_flow") or {}).get(comp_idx) or {})
            expname = ctx["expname"]
            fpath = ctx["fpath"]
        if not comp_flow:
            print(f"[DataLogger] Warning: no stored flow data for exp={exp_id} comp={comp_idx}")
            return

        flow_data = {
            "time": list(comp_flow.get("time_data") or []),
            "flows": list(comp_flow.get("flows") or [[], [], [], []]),
            "pressures_set": list(comp_flow.get("pressures_set") or [[], [], [], []]),
            "pressures_act": list(comp_flow.get("pressures_act") or [[], [], [], []]),
            "extra_flow": list(comp_flow.get("extra_flow") or []),
            "extra_pressure_set": list(comp_flow.get("extra_pressure_set") or []),
            "extra_pressure_act": list(comp_flow.get("extra_pressure_act") or []),
            "set_FR": list(comp_flow.get("set_FR") or []),
        }
        active_channels = list(p.get("active_channels") or [1, 2, 3, 4])
        set_fr_active = list(flow_data.get("set_FR") or [])
        # Align setpoints to full physical channel order [ch1..ch4] for correct stats/errors.
        set_fr_full = [0.0, 0.0, 0.0, 0.0]
        for idx, ch in enumerate(active_channels):
            try:
                ch_i = int(ch)
            except Exception:
                continue
            if 1 <= ch_i <= 4 and idx < len(set_fr_active):
                set_fr_full[ch_i - 1] = float(set_fr_active[idx])
        flow_data["set_FR"] = list(set_fr_full)
        flowstart = int(comp_flow.get("flowstart", 0))
        stats = records.save_flow_data(
            expname=expname,
            fpath=fpath,
            comp_idx=comp_idx,
            flow_data=flow_data,
            flowstart=flowstart,
            exp_id=exp_id,
        )

        output_wells = list(p.get("output_wells") or [])
        if comp_idx < len(output_wells):
            well = output_wells[comp_idx]
            plate, row, col = int(well[0]), int(well[1]), int(well[2])
        else:
            plate, row, col = 1, 1, 1

        set_fr = list(set_fr_full or (p.get("flow_rates_exec") or p.get("flow_rates") or [[0, 0, 0, 0]])[0])
        compositions = list(p.get("compositions") or [])
        composition = list(compositions[comp_idx]) if comp_idx < len(compositions) else [0.0, 0.0, 0.0]
        eq_t = 0.0
        tdat = flow_data["time"]
        if tdat and 0 <= flowstart < len(tdat):
            eq_t = float(max(0.0, tdat[flowstart] - tdat[0]))
        expul_t = self._estimate_expul_time_s(list(p.get("tubingdim") or [0.51, 750]), set_fr)

        lipid_stocks = list(p.get("lipid_stocks") or [])
        lipid_names = [str(x.get("name", f"Lp{i+1}")) for i, x in enumerate(lipid_stocks[:3])]
        lipid_comps = list(composition[:len(lipid_names)])
        slot_to_line = list(p.get("runtime_slot_to_line") or [i + 1 for i in range(len(lipid_names))])
        mean_full = list(stats.get("mean_fr", [0, 0, 0, 0]))
        std_full = list(stats.get("stdev_fr", [0, 0, 0, 0]))
        lipid_frs = []
        lip_means = []
        lip_stds = []
        lip_errs = []
        for slot_idx in range(len(lipid_names)):
            line = int(slot_to_line[slot_idx]) if slot_idx < len(slot_to_line) else (slot_idx + 1)
            fr_set = float(set_fr[line]) if line < len(set_fr) else 0.0  # line1->ch2 idx1 etc.
            fr_mean = float(mean_full[line]) if line < len(mean_full) else 0.0
            fr_std = float(std_full[line]) if line < len(std_full) else 0.0
            lipid_frs.append(fr_set)
            lip_means.append(fr_mean)
            lip_stds.append(fr_std)
            lip_errs.append(abs(fr_mean - fr_set))

        buffer_cfg = p.get("buffer") or {}
        records.save_to_excel(
            fpath=fpath,
            expname=expname,
            exp_name=str(p.get("exp_name", exp_id)),
            status="Complete",
            comp_idx=comp_idx,
            composition=composition,
            flow_rates=set_fr,
            plate=plate,
            row=row,
            col=col,
            volume=volume_collected,
            eq_t=eq_t,
            expul_t=expul_t,
            buffer_name=str(buffer_cfg.get("name", "Buffer")),
            buffer_fr=float(set_fr[0]) if set_fr else 0.0,
            buf_fr_err=float(stats.get("buffer_fr_err", 0.0)),
            buf_fr_mean=float(stats.get("mean_fr", [0])[0] if stats.get("mean_fr") else 0.0),
            buf_fr_std=float(stats.get("stdev_fr", [0])[0] if stats.get("stdev_fr") else 0.0),
            lipid_names=lipid_names,
            lipid_comps=lipid_comps,
            lipid_frs=lipid_frs,
            lip_fr_errs=lip_errs,
            lip_fr_means=lip_means,
            lip_fr_stds=lip_stds,
            active_channels=active_channels,
            repeat_num=1,
        )

    def _estimate_expul_time_s(self, tubingdim: List[float], set_fr: List[float]) -> float:
        try:
            area = float(np.pi) * (float(tubingdim[0]) / 2.0) ** 2
            tube_vol = area * (float(tubingdim[1]) * 1.2)
            total_fr = float(sum(float(x) for x in set_fr))
            if total_fr <= 0:
                return 0.0
            return float(tube_vol / (total_fr / 60.0))
        except Exception:
            return 0.0

    def _json_default(self, obj: Any):
        """Fallback JSON serializer for numpy types and other non-serializables."""
        try:
            import numpy as np
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, (np.ndarray,)):
                return obj.tolist()
        except Exception:
            pass
        return str(obj)

    def _init_run_timeseries_csv(self, csv_path: str) -> None:
        header = [
            "timestamp_iso",
            "timestamp_epoch",
            "elapsed_s",
            "exp_id",
            "flow_ch1",
            "flow_ch2",
            "flow_ch3",
            "flow_ch4",
            "pressure_set_ch1",
            "pressure_set_ch2",
            "pressure_set_ch3",
            "pressure_set_ch4",
            "pressure_act_ch1",
            "pressure_act_ch2",
            "pressure_act_ch3",
            "pressure_act_ch4",
            "flow_extra",
            "pressure_set_extra",
            "pressure_act_extra",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)

    def _append_run_timeseries_row(
        self,
        exp_id: str,
        timestamp: float,
        flows: List[float],
        pressures_act: List[float],
        pressures_set: List[float],
        extra_flow: Optional[float] = None,
        extra_pressure_act: Optional[float] = None,
        extra_pressure_set: Optional[float] = None,
    ) -> None:
        with self._lock:
            run_ctx = dict(self._run_ctx or {})
        if not run_ctx.get("active"):
            return
        csv_path = str(run_ctx.get("run_flow_csv") or "")
        if not csv_path:
            return

        started_epoch = float(run_ctx.get("started_epoch") or timestamp)

        def _pad4(vals: List[float]) -> List[float]:
            out = [0.0, 0.0, 0.0, 0.0]
            for i in range(min(4, len(vals or []))):
                try:
                    out[i] = float(vals[i])
                except Exception:
                    out[i] = 0.0
            return out

        fl = _pad4(list(flows or []))
        ps = _pad4(list(pressures_set or []))
        pa = _pad4(list(pressures_act or []))
        row = [
            datetime.fromtimestamp(timestamp).isoformat(),
            float(timestamp),
            float(timestamp - started_epoch),
            str(exp_id or ""),
            *fl,
            *ps,
            *pa,
            "" if extra_flow is None else float(extra_flow),
            "" if extra_pressure_set is None else float(extra_pressure_set),
            "" if extra_pressure_act is None else float(extra_pressure_act),
        ]
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)

    def _generate_run_timeseries_plots(self, run_dir: str, csv_path: Optional[str]) -> Dict[str, str]:
        if not csv_path or not os.path.exists(csv_path):
            return {}

        t: List[float] = []
        flow_ch = [[], [], [], []]
        p_act_ch = [[], [], [], []]
        flow_extra: List[float] = []
        p_act_extra: List[float] = []

        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    t.append(float(row.get("elapsed_s", "0") or 0.0))
                    for i in range(4):
                        flow_ch[i].append(float(row.get(f"flow_ch{i+1}", "0") or 0.0))
                        p_act_ch[i].append(float(row.get(f"pressure_act_ch{i+1}", "0") or 0.0))
                    flow_extra.append(float(row.get("flow_extra", "") or 0.0))
                    p_act_extra.append(float(row.get("pressure_act_extra", "") or 0.0))
                except Exception:
                    continue

        if not t:
            return {}

        # Re-zero run plots at first PID-controlled sample so x-axis starts at t=0
        # when active flow control begins (typically first priming/flush composition),
        # not at run-start/queue setup time.
        t0 = float(t[0])
        t = [float(v - t0) for v in t]

        plots_dir = os.path.join(run_dir, "Flowplots")
        os.makedirs(plots_dir, exist_ok=True)
        flow_svg = os.path.join(plots_dir, "run_flow_rates.svg")
        pressure_svg = os.path.join(plots_dir, "run_pressures.svg")
        extra_present = any(abs(v) > 0.0 for v in flow_extra) or any(abs(v) > 0.0 for v in p_act_extra)
        flow_series = flow_ch + ([flow_extra] if extra_present else [])
        pressure_series = p_act_ch + ([p_act_extra] if extra_present else [])
        flow_names = ["Flow ch1", "Flow ch2", "Flow ch3", "Flow ch4"] + (["Flow RNA extra"] if extra_present else [])
        pressure_names = (
            ["Pressure ch1", "Pressure ch2", "Pressure ch3", "Pressure ch4"]
            + (["Pressure RNA extra"] if extra_present else [])
        )
        self._write_simple_svg_plot(
            out_path=flow_svg,
            title="Run Flow Rates (Full Run)",
            x=t,
            ys=flow_series,
            y_label="Flow (uL/min)",
            series_names=flow_names,
        )
        self._write_simple_svg_plot(
            out_path=pressure_svg,
            title="Run Pressures (Full Run)",
            x=t,
            ys=pressure_series,
            y_label="Pressure (mbar)",
            series_names=pressure_names,
        )
        return {
            "flow_plot": os.path.relpath(flow_svg, run_dir).replace("\\", "/"),
            "pressure_plot": os.path.relpath(pressure_svg, run_dir).replace("\\", "/"),
        }

    def _write_simple_svg_plot(
        self,
        out_path: str,
        title: str,
        x: List[float],
        ys: List[List[float]],
        y_label: str,
        series_names: List[str],
    ) -> None:
        if not x:
            return
        W, H = 1400, 700
        ML, MR, MT, MB = 90, 30, 50, 70
        plot_w = W - ML - MR
        plot_h = H - MT - MB
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#50dc8c"]

        x_min = min(x)
        x_max = max(x)
        if x_max <= x_min:
            x_max = x_min + 1.0
        y_vals = [v for ch in ys for v in ch] or [0.0]
        y_min = min(y_vals)
        y_max = max(y_vals)
        if y_max <= y_min:
            y_max = y_min + 1.0
        pad = 0.05 * (y_max - y_min)
        y_min -= pad
        y_max += pad

        def sx(v: float) -> float:
            return ML + (v - x_min) / (x_max - x_min) * plot_w

        def sy(v: float) -> float:
            return MT + (1.0 - (v - y_min) / (y_max - y_min)) * plot_h

        def esc(s: str) -> str:
            return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines: List[str] = []
        lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
        lines.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
        lines.append(f'<text x="{W//2}" y="30" text-anchor="middle" font-family="Arial" font-size="22">{esc(title)}</text>')

        nx, ny = 10, 8
        for i in range(nx + 1):
            xx = ML + i * plot_w / nx
            xv = x_min + i * (x_max - x_min) / nx
            lines.append(f'<line x1="{xx:.2f}" y1="{MT}" x2="{xx:.2f}" y2="{MT+plot_h}" stroke="#e6e6e6"/>')
            lines.append(f'<text x="{xx:.2f}" y="{MT+plot_h+24}" text-anchor="middle" font-family="Arial" font-size="12" fill="#555">{xv:.1f}</text>')
        for j in range(ny + 1):
            yy = MT + j * plot_h / ny
            yv = y_max - j * (y_max - y_min) / ny
            lines.append(f'<line x1="{ML}" y1="{yy:.2f}" x2="{ML+plot_w}" y2="{yy:.2f}" stroke="#e6e6e6"/>')
            lines.append(f'<text x="{ML-10}" y="{yy+4:.2f}" text-anchor="end" font-family="Arial" font-size="12" fill="#555">{yv:.1f}</text>')

        lines.append(f'<line x1="{ML}" y1="{MT+plot_h}" x2="{ML+plot_w}" y2="{MT+plot_h}" stroke="#333" stroke-width="1.5"/>')
        lines.append(f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{MT+plot_h}" stroke="#333" stroke-width="1.5"/>')
        lines.append(f'<text x="{ML+plot_w/2:.2f}" y="{H-20}" text-anchor="middle" font-family="Arial" font-size="14">Elapsed time (s)</text>')
        lines.append(f'<text x="25" y="{MT+plot_h/2:.2f}" transform="rotate(-90 25 {MT+plot_h/2:.2f})" text-anchor="middle" font-family="Arial" font-size="14">{esc(y_label)}</text>')

        for i, series in enumerate(ys):
            if not series:
                continue
            n = min(len(x), len(series))
            points = " ".join(f"{sx(x[k]):.2f},{sy(series[k]):.2f}" for k in range(n))
            lines.append(f'<polyline fill="none" stroke="{colors[i % len(colors)]}" stroke-width="1.6" points="{points}"/>')

        lx, ly = W - 240, 80
        legend_h = 42 + 24 * max(1, len(series_names))
        lines.append(f'<rect x="{lx-15}" y="{ly-25}" width="210" height="{legend_h}" fill="white" stroke="#ddd"/>')
        for i in range(len(series_names)):
            yy = ly + i * 24
            lines.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+30}" y2="{yy}" stroke="{colors[i % len(colors)]}" stroke-width="3"/>')
            lines.append(f'<text x="{lx+40}" y="{yy+4}" font-family="Arial" font-size="13">{esc(series_names[i])}</text>')

        lines.append("</svg>")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _update_daily_manifest(
        self,
        run_id: Any,
        run_name: str,
        run_dir: str,
        started_at: str,
        ended_at: str,
        run_flow_csv: str,
        plot_outputs: Dict[str, str],
    ) -> None:
        try:
            day = datetime.now().strftime("%Y%m%d")
            day_dir = Path("./logs") / day
            day_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = day_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    manifest = {}
            else:
                manifest = {}

            manifest.setdefault("date", day)
            manifest["updated_at"] = datetime.now().isoformat()
            manifest.setdefault("runs", [])
            manifest.setdefault("experiments", [])
            manifest.setdefault("files", [])

            run_entry = {
                "run_id": int(run_id) if str(run_id).isdigit() else run_id,
                "run_name": run_name,
                "run_dir": run_dir,
                "started_at": started_at,
                "ended_at": ended_at,
                "run_flow_csv": run_flow_csv,
                "run_plots": dict(plot_outputs or {}),
            }
            updated = False
            for i, row in enumerate(manifest["runs"]):
                if str(row.get("run_id")) == str(run_entry["run_id"]):
                    manifest["runs"][i] = run_entry
                    updated = True
                    break
            if not updated:
                manifest["runs"].append(run_entry)

            exp_files = sorted(p.name for p in day_dir.glob("exp_*.json"))
            manifest["experiments"] = exp_files
            file_list = []
            for p in sorted(day_dir.glob("*")):
                if p.is_file():
                    file_list.append(p.name)
            manifest["files"] = file_list

            manifest_path.write_text(json.dumps(manifest, indent=2, default=self._json_default), encoding="utf-8")
        except Exception as e:
            print(f"[DataLogger] Warning: could not update daily manifest: {e}")

    def _update_runs_index(
        self,
        run_id: Any,
        run_name: str,
        run_dir: str,
        started_at: str,
        ended_at: str,
        run_flow_csv: str,
        plot_outputs: Dict[str, str],
        queue_count: int = 0,
    ) -> None:
        try:
            index_path = Path("./logs") / "runs_index.csv"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            headers = [
                "run_id",
                "run_name",
                "run_dir",
                "started_at",
                "ended_at",
                "queue_count",
                "run_flow_csv",
                "flow_plot",
                "pressure_plot",
            ]
            row = {
                "run_id": str(run_id),
                "run_name": str(run_name or ""),
                "run_dir": str(run_dir or ""),
                "started_at": str(started_at or ""),
                "ended_at": str(ended_at or ""),
                "queue_count": str(int(queue_count or 0)),
                "run_flow_csv": str(run_flow_csv or ""),
                "flow_plot": str((plot_outputs or {}).get("flow_plot", "")),
                "pressure_plot": str((plot_outputs or {}).get("pressure_plot", "")),
            }

            rows: List[Dict[str, str]] = []
            if index_path.exists():
                with open(index_path, "r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        rows.append({k: str(r.get(k, "")) for k in headers})

            replaced = False
            for i, r in enumerate(rows):
                if str(r.get("run_id", "")) == str(row["run_id"]):
                    rows[i] = row
                    replaced = True
                    break
            if not replaced:
                rows.append(row)

            rows.sort(key=lambda r: int(r["run_id"]) if str(r.get("run_id", "")).isdigit() else 0)
            with open(index_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(rows)
        except Exception as e:
            print(f"[DataLogger] Warning: could not update runs index: {e}")
