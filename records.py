import numpy as np
from datetime import datetime
import os
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils.dataframe import dataframe_to_rows
import json
from typing import Dict, List, Optional, Any

def _json_dumps_safe(value: Any) -> str:
    """JSON-dump with best-effort conversion for numpy/custom objects."""
    def _default(obj: Any):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        return str(obj)
    return json.dumps(value, default=_default)


def get_next_id():
    """Get next experiment ID from counter file."""
    id_file = "./id.npy"
    if os.path.exists(id_file):
        try:
            previd = int(np.load(id_file, allow_pickle=True))
        except Exception as e:
            print(f"[Records] Warning: could not read {id_file}; resetting counter to 0 ({e})")
            previd = 0
    else:
        previd = 0
    next_id = previd + 1
    np.save(id_file, next_id)
    return next_id


def get_next_run_id():
    """Get next run ID from counter file."""
    id_file = "./run_id.npy"
    if os.path.exists(id_file):
        try:
            prev = int(np.load(id_file, allow_pickle=True))
        except Exception as e:
            print(f"[Records] Warning: could not read {id_file}; resetting counter to 0 ({e})")
            prev = 0
    else:
        prev = 0
    next_id = prev + 1
    np.save(id_file, next_id)
    return int(next_id)


def get_date():
    """Get current date in YYMMDD format."""
    now = datetime.now()
    date = now.strftime("%y%m%d")
    return date


def get_time():
    """Get current time in HH:MM:SS format."""
    now = datetime.now()
    time = now.strftime("%H:%M:%S")
    return time


def create_experiment_folder(exp_id: str, exp_name: str, root_dir: str = "./FlowData") -> str:
    """Create experiment folder and return path."""
    now = datetime.now()
    date = now.strftime("%y%m%d")
    
    # Format: {id}-{date}-{name}
    expname = f"{exp_id}-{date}-{exp_name}"
    
    print(f"Creating experiment folder: {expname}")
    
    fpath = f'{root_dir}/{expname}'
    
    if not os.path.exists(fpath):
        os.makedirs(fpath)
        os.makedirs(f'{fpath}/Flowplots', exist_ok=True)
    
    return expname, fpath


def save_parameter_record(
    expname: str,
    fpath: str,
    exp_id: str,
    exp_name: str,
    details: str,
    buffer_name: str,
    buffer_params: Dict,
    buffer_notes: str,
    lipid_stocks: List[Dict],
    lipid_notes: List[str],
    compositions: List[List[float]],
    flow_rates: List[List[float]],
    inst_name: str,
    active_chans: List[int],
    sensorcorr: List[List[float]],
    volume: float,
    repeats: int,
    period: float,
    K_p: List[float],
    K_i: float,
    p_incr: List[float],
    p_range: List[float],
    max_eq_t: float,
    eq_abs_error: List[float],
    tfr: float,
    frr: float,
    screen_space_mode: str,
    screen_space_params: Dict,
    app_config: Optional[Dict[str, Any]] = None,
    flow_rates_exec: Optional[List[List[float]]] = None,
    runtime_slot_to_line: Optional[List[int]] = None,
    num_plates: int = 1
) -> None:
    """Save comprehensive parameter record as CSV."""
    
    now = datetime.now()
    date = now.strftime("%y%m%d")
    time_str = now.strftime("%H:%M:%S")
    
    # Extract lipid parameters (up to 3 lipids)
    lipid_data = {}
    for i, lipid in enumerate(lipid_stocks[:3], 1):
        lipid_data[f'Lp{i}Name'] = [lipid.get('name', 'N/A')]
        lipid_data[f'Lp{i}Conc'] = [lipid.get('concentration', 0)]
        lipid_data[f'Lp{i}MW'] = [lipid.get('mw', 0)]
        lipid_data[f'Lp{i}Notes'] = [lipid_notes[i-1] if i-1 < len(lipid_notes) else '']
    
    # Fill remaining lipid slots if less than 3
    for i in range(len(lipid_stocks) + 1, 4):
        lipid_data[f'Lp{i}Name'] = ['N/A']
        lipid_data[f'Lp{i}Conc'] = [0]
        lipid_data[f'Lp{i}MW'] = [0]
        lipid_data[f'Lp{i}Notes'] = ['']
    
    data = {
        'exp_id': [exp_id],
        'Date': [date],
        'Time': [time_str],
        'expname': [expname],
        'exp_name': [exp_name],
        'Details': [details],
        'num_plates': [num_plates],
        'BufferName': [buffer_name],
        'BufferParams': [_json_dumps_safe(buffer_params)],
        'BufferNotes': [buffer_notes],
        **lipid_data,
        'compositions': [_json_dumps_safe(compositions)],
        'flow_rates': [_json_dumps_safe(flow_rates)],
        'flow_rates_exec': [_json_dumps_safe(flow_rates_exec or [])],
        'inst_name': [inst_name],
        'active_chans': [_json_dumps_safe(active_chans)],
        'runtime_slot_to_line': [_json_dumps_safe(runtime_slot_to_line or [])],
        'sensorcorr': [_json_dumps_safe(sensorcorr)],
        'volume': [volume],
        'repeats': [repeats],
        'period': [period],
        'K_p': [_json_dumps_safe(K_p.tolist() if isinstance(K_p, np.ndarray) else K_p)],
        'K_i': [K_i],
        'p_incr': [_json_dumps_safe(p_incr)],
        'p_range': [_json_dumps_safe(p_range)],
        'max_eq_t': [max_eq_t],
        'eq_abs_error': [_json_dumps_safe(eq_abs_error)],
        'TFR': [tfr],
        'FRR': [frr],
        'screen_space_mode': [screen_space_mode],
        'screen_space_params': [_json_dumps_safe(screen_space_params)],
        'app_config': [_json_dumps_safe(app_config or {})],
    }
    
    df = pd.DataFrame(data)
    fname = f'{fpath}/param_record-{expname}'
    df_transposed = df.T
    df_transposed.to_csv(f"{fname}.csv")
    print(f"Parameter record saved to {fname}.csv")


def save_flow_data(
    expname: str,
    fpath: str,
    comp_idx: int,
    flow_data: Dict,
    flowstart: int,
    exp_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Save flow data for a single composition and return statistics."""
    
    try:
        # Extract steady-state flow data (after equilibration)
        steady_flows = []
        for channel_data in flow_data['flows']:
            steady_flows.append(channel_data[flowstart:])
        
        # Calculate statistics
        mean_fr = [np.mean(data) if len(data) > 0 else 0 for data in steady_flows]
        stdev_fr = [np.std(data) if len(data) > 0 else 0 for data in steady_flows]
        
        # Calculate errors - set_FR is for active channels only, pad with zeros if needed
        set_FR_full = flow_data['set_FR'] + [0.0] * (len(mean_fr) - len(flow_data['set_FR']))
        buffer_fr_err = abs(mean_fr[0] - set_FR_full[0])
        lipid_fr_err = [abs(mean_fr[i] - set_FR_full[i]) for i in range(1, len(mean_fr))]
        
        # Save full flow data (append to one per-experiment file under logs/YYYYMMDD)
        log_date_dir = f"./logs/{datetime.now().strftime('%Y%m%d')}"
        os.makedirs(log_date_dir, exist_ok=True)
        exp_key = str(exp_id or expname or "experiment")
        full_csv = f"{log_date_dir}/{exp_key}_flowdata_full.csv"
        steady_csv = f"{log_date_dir}/{exp_key}_flowdata_steady.csv"
        n_time = len(flow_data['time'])

        def _series(name: str) -> List[Any]:
            vals = list(flow_data.get(name) or [])
            return vals[:n_time] + [None] * max(0, n_time - len(vals))

        extra_flow = _series("extra_flow")
        extra_pressure_set = _series("extra_pressure_set")
        extra_pressure_act = _series("extra_pressure_act")

        flow_df = pd.DataFrame({
            'exp_id': [exp_key] * n_time,
            'expname': [expname] * n_time,
            'comp_idx': [int(comp_idx)] * n_time,
            'time': flow_data['time'],
            'flow_ch1': flow_data['flows'][0],
            'flow_ch2': flow_data['flows'][1],
            'flow_ch3': flow_data['flows'][2],
            'flow_ch4': flow_data['flows'][3],
            'flow_extra': extra_flow,
            'pressure_set_ch1': flow_data['pressures_set'][0],
            'pressure_set_ch2': flow_data['pressures_set'][1],
            'pressure_set_ch3': flow_data['pressures_set'][2],
            'pressure_set_ch4': flow_data['pressures_set'][3],
            'pressure_set_extra': extra_pressure_set,
            'pressure_act_ch1': flow_data['pressures_act'][0],
            'pressure_act_ch2': flow_data['pressures_act'][1],
            'pressure_act_ch3': flow_data['pressures_act'][2],
            'pressure_act_ch4': flow_data['pressures_act'][3],
            'pressure_act_extra': extra_pressure_act,
        })
        flow_df.to_csv(full_csv, mode='a', header=not os.path.exists(full_csv), index=False)
        print(f"[Records] Appended full flow data: {os.path.basename(full_csv)} (comp {comp_idx})")
        
        # Save steady-state only (append to one per-experiment file under logs/YYYYMMDD)
        steady_len = len(steady_flows[0]) if steady_flows and len(steady_flows[0]) else 0
        steady_df = pd.DataFrame({
            'exp_id': [exp_key] * steady_len,
            'expname': [expname] * steady_len,
            'comp_idx': [int(comp_idx)] * steady_len,
            'steady_idx': list(range(steady_len)),
            'flow_ch1': steady_flows[0],
            'flow_ch2': steady_flows[1],
            'flow_ch3': steady_flows[2],
            'flow_ch4': steady_flows[3],
            'flow_extra': extra_flow[flowstart:],
            'pressure_set_extra': extra_pressure_set[flowstart:],
            'pressure_act_extra': extra_pressure_act[flowstart:],
        })
        steady_df.to_csv(steady_csv, mode='a', header=not os.path.exists(steady_csv), index=False)
        print(f"[Records] Appended steady flow data: {os.path.basename(steady_csv)} (comp {comp_idx})")
        
        return {
            'mean_fr': mean_fr,
            'stdev_fr': stdev_fr,
            'buffer_fr_err': buffer_fr_err,
            'lipid_fr_err': lipid_fr_err
        }
    except Exception as e:
        print(f"[Records] ERROR saving flow data for comp {comp_idx}: {e}")
        import traceback
        traceback.print_exc()
        # Return default values on error
        return {
            'mean_fr': [0, 0, 0, 0],
            'stdev_fr': [0, 0, 0, 0],
            'buffer_fr_err': 0,
            'lipid_fr_err': [0, 0, 0]
        }


def save_to_excel(
    fpath: str,
    expname: str,
    exp_name: str,
    status: str,
    comp_idx: int,
    composition: List[float],
    flow_rates: List[float],
    plate: int,
    row: int,
    col: int,
    volume: float,
    eq_t: float,
    expul_t: float,
    buffer_name: str,
    buffer_fr: float,
    buf_fr_err: float,
    buf_fr_mean: float,
    buf_fr_std: float,
    lipid_names: List[str],
    lipid_comps: List[float],
    lipid_frs: List[float],
    lip_fr_errs: List[float],
    lip_fr_means: List[float],
    lip_fr_stds: List[float],
    active_channels: List[int],
    repeat_num: int = 1
) -> None:
    """Append composition result to Excel log."""
    
    TotalFR = np.sum(flow_rates)
    FRR = flow_rates[0] / np.sum(flow_rates[1:]) if np.sum(flow_rates[1:]) > 0 else 0
    
    # Build data dict based on active channels
    data = {
        'ExpName': [str(exp_name)],
        'State': [status],
        'CompIdx': [comp_idx + 1],
        'RepeatNum': [repeat_num],
        'Time': [datetime.now().strftime('%H:%M:%S')],
        'Date': [datetime.today().strftime('%Y-%m-%d')],
        'Plate': [plate],
        'Row': [row],
        'Col': [col],
        'WPIndex': [f"P{plate}R{row}C{col}"],
        'FRR': [FRR],
        'TotalFR': [TotalFR],
        'Volume': [volume],
        'Eq_Time': [eq_t],
        'Expul_Time': [expul_t],
        'Buf-Name': [buffer_name],
        'Buf-FR': [buffer_fr],
        'Buf-FRer': [buf_fr_err],
        'Buf-FRmean': [buf_fr_mean],
        'Buf-FRstd': [buf_fr_std]
    }
    
    # Add lipid data for each active lipid channel
    for i, lipid_name in enumerate(lipid_names):
        data[f'Lp{i+1}-Name'] = [lipid_name]
        data[f'Lp{i+1}-Comp'] = [lipid_comps[i] if i < len(lipid_comps) else 0]
        data[f'Lp{i+1}-FR'] = [lipid_frs[i] if i < len(lipid_frs) else 0]
        data[f'Lp{i+1}-FRer'] = [lip_fr_errs[i] if i < len(lip_fr_errs) else 0]
        data[f'Lp{i+1}-FRmean'] = [lip_fr_means[i] if i < len(lip_fr_means) else 0]
        data[f'Lp{i+1}-FRstd'] = [lip_fr_stds[i] if i < len(lip_fr_stds) else 0]
    
    # Convert to DataFrame
    df = pd.DataFrame(data)
    
    # Excel file name
    excel_file = f"{fpath}/{expname}_explog.xlsx"
    
    try:
        # Load existing workbook and append
        book = load_workbook(excel_file)
        sheet = book.active
        
        for r in dataframe_to_rows(df, index=False, header=False):
            sheet.append(r)
        
        book.save(excel_file)
        print(f"[Records] Data appended to Excel: {excel_file}")
        
    except FileNotFoundError:
        # Create new Excel file with header
        try:
            df.to_excel(excel_file, index=False)
            print(f"[Records] Created new Excel file: {excel_file}")
        except Exception as e:
            print(f"[Records] WARNING: Could not create Excel file: {e}")
            # Fallback: save as CSV
            csv_file = f"{fpath}/{expname}_explog.csv"
            df.to_csv(csv_file, mode='a', header=not os.path.exists(csv_file), index=False)
            print(f"[Records] Saved to CSV instead: {csv_file}")
    except Exception as e:
        print(f"[Records] WARNING: Excel append failed: {e}")
        # Fallback: save as CSV
        csv_file = f"{fpath}/{expname}_explog.csv"
        try:
            df.to_csv(csv_file, mode='a', header=not os.path.exists(csv_file), index=False)
            print(f"[Records] Saved to CSV fallback: {csv_file}")
        except Exception as e2:
            print(f"[Records] ERROR: Could not save data: {e2}")


def initiate_folder(path: str) -> None:
    """Create folder if it doesn't exist."""
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"Directory '{path}' created successfully.")
    else:
        print(f"Directory '{path}' already exists.")
