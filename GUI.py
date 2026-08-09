from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QGroupBox, QComboBox, QLineEdit, QDialog,
    QMessageBox, QTreeWidget, QTreeWidgetItem, QFrame, QSpinBox,
    QDoubleSpinBox, QFileDialog, QCheckBox, QColorDialog, QProgressBar,
    QScrollArea, QTableWidget, QTableWidgetItem as QTableItem, QTabWidget,
    QFormLayout
)
from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QPalette, QColor, QIcon, QPainter, QPen, QBrush
import pyqtgraph as pg
import numpy as np
import re
import threading
import serial.tools.list_ports
import sys
import time
import traceback

class IntakeWellWidget(QWidget):
    """Single circular well for intake array with nozzle indicator."""
    def __init__(self, plate, row, col, parent=None, on_click=None):
        super().__init__(parent)
        self.plate = plate
        self.row = row
        self.col = col
        self.lipid_name = None
        self.lipid_color = "#555555"
        self.active_nozzle = None
        self.on_click = on_click
        self.setFixedSize(40, 40)
        self.setToolTip("")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_lipid(self, name, color):
        self.lipid_name = name
        self.lipid_color = color
        self.setToolTip(name if name else "Empty")
        self.update()

    def set_active_nozzle(self, nozzle):
        self.active_nozzle = nozzle
        self.update()

    def mousePressEvent(self, event):
        if self.on_click:
            self.on_click(self.plate, self.row, self.col)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw lipid circle
        painter.setBrush(QBrush(QColor(self.lipid_color)))
        painter.setPen(QPen(Qt.GlobalColor.white, 1))
        painter.drawEllipse(8, 8, 24, 24)

        # Draw nozzle indicator ring
        if self.active_nozzle:
            nozzle_colors = {1: "#FF6B6B", 2: "#4ECDC4", 3: "#FFD93D"}
            painter.setPen(QPen(QColor(nozzle_colors[self.active_nozzle]), 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(4, 4, 32, 32)

class OutputPlateWidget(QWidget):
    """96-well output plate visualization."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_plate = 1
        self.wells = {}  # (plate, row, col) -> color
        self.well_opacity = {}  # (plate, row, col) -> opacity (0.0-1.0), default 1.0 for complete, 0.15 for preview
        self.well_info = {}  # (plate, row, col) -> {"exp_name": str, "composition": [list]}
        self.selected_well = (1, 1, 1)  # plate, row, col
        self.hovered_well = None  # currently hovered well
        self.on_select = None
        self.setMinimumSize(500, 330)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)  # Enable mouseMoveEvent

    def set_well_color(self, plate, row, col, color, is_preview=False, exp_name=None, composition=None):
        """Set well color. is_preview=True shows well as transparent (planned), False is opaque (completed)."""
        self.wells[(plate, row, col)] = color
        self.well_opacity[(plate, row, col)] = 0.08 if is_preview else 1.0  # 0.08 (8%) for preview, 1.0 (100%) for completed
        if exp_name or composition:
            self.well_info[(plate, row, col)] = {
                "exp_name": exp_name or "Unknown",
                "composition": composition or []
            }
        if plate == self.current_plate:
            self.update()

    def mark_well_complete(self, plate, row, col):
        """Mark a preview well as actually complete (make opaque)."""
        if (plate, row, col) in self.wells:
            self.well_opacity[(plate, row, col)] = 1.0
            if plate == self.current_plate:
                self.update()

    def set_plate(self, plate):
        self.current_plate = plate
        self.update()

    def mouseMoveEvent(self, event):
        """Handle hover for tooltip display."""
        x = event.position().x()
        y = event.position().y()
        well_size = 24
        spacing = 5
        start_x, start_y = 40, 10
        
        hovered = None
        for row in range(8):
            for col in range(12):
                wx = start_x + col * (well_size + spacing)
                wy = start_y + row * (well_size + spacing)
                if wx <= x <= wx + well_size and wy <= y <= wy + well_size:
                    well_pos = (self.current_plate, row + 1, col + 1)
                    hovered = well_pos
                    
                    # Show tooltip if well has info
                    if well_pos in self.well_info:
                        info = self.well_info[well_pos]
                        comp = info.get("composition", [])
                        comp_str = f"{comp[0]:.1f}% {comp[1]:.1f}% {comp[2]:.1f}%" if len(comp) >= 3 else "Unknown"
                        tooltip = f"{info['exp_name']}\n{comp_str}"
                        self.setToolTip(tooltip)
                    else:
                        self.setToolTip("")
                    break
        
        if hovered != self.hovered_well:
            self.hovered_well = hovered
            self.update()

    def mousePressEvent(self, event):
        x = event.position().x()
        y = event.position().y()
        well_size = 24
        spacing = 5
        start_x, start_y = 40, 10

        for row in range(8):
            for col in range(12):
                rx = start_x + col * (well_size + spacing)
                ry = start_y + row * (well_size + spacing)
                if rx <= x <= rx + well_size and ry <= y <= ry + well_size:
                    self.selected_well = (self.current_plate, row + 1, col + 1)
                    self.update()
                    if self.on_select:
                        self.on_select(self.selected_well)
                    break

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Draw 8x12 grid - smaller well size, aligned to top
        well_size = 24
        spacing = 5
        start_x, start_y = 40, 10
        
        # Draw row labels (A-H)
        painter.setPen(QPen(Qt.GlobalColor.white, 1))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        for row in range(8):
            y = start_y + row * (well_size + spacing) + well_size // 2
            label = chr(65 + row)  # A-H
            painter.drawText(10, y + 5, label)
        
        # Draw column labels (1-12)
        for col in range(12):
            x = start_x + col * (well_size + spacing) + well_size // 2
            label = str(col + 1)
            # Center the label
            text_width = painter.fontMetrics().horizontalAdvance(label)
            painter.drawText(x - text_width // 2, start_y - 3, label)

        for row in range(8):
            for col in range(12):
                x = start_x + col * (well_size + spacing)
                y = start_y + row * (well_size + spacing)

                color_str = self.wells.get((self.current_plate, row + 1, col + 1), "#FFFFFF")
                opacity = self.well_opacity.get((self.current_plate, row + 1, col + 1), 1.0)
                
                # Parse color and apply opacity
                qcolor = QColor(color_str)
                qcolor.setAlphaF(opacity)
                painter.setBrush(QBrush(qcolor))
                painter.setPen(QPen(Qt.GlobalColor.gray, 1))
                painter.drawEllipse(x, y, well_size, well_size)

                if self.selected_well == (self.current_plate, row + 1, col + 1):
                    painter.setPen(QPen(Qt.GlobalColor.white, 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(x - 2, y - 2, well_size + 4, well_size + 4)

class MainGUI(QMainWindow):
    def __init__(self, control_api):
        super().__init__()
        self.control_api = control_api
        self._last_error = None
        self._last_recovery_prompt = None
        self._recovery_popup_open = False
        self._conn_state = {"Microfluidics": False, "Dobot": False, "Microcontroller": False}
        self._buffer_selected = False
        self._last_status_error_t = 0.0
        self._last_intake_alloc_sig = None
        self._queue_finished_cleanup_prompted = False
        self.setWindowTitle("Microfluidic Robotic Platform")
        self.setWindowIcon(QIcon("Gemini_Generated_Image_lxjg5xlxjg5xlxjg.png"))
        self.resize(1600, 1000)
        self._apply_dark_theme()
        self._build_layout()
        self._refresh_selected_config_label()
        
        # Status update timer
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(500)

        # Resume checkpoint prompt
        QTimer.singleShot(250, self._prompt_resume_checkpoint)


    def _apply_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Highlight, QColor(142, 45, 197))
        palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
        self.setPalette(palette)
        self.setStyleSheet("QPushButton { font-weight: bold; }")

    def _build_layout(self):
        root = QWidget()
        root.setStyleSheet(
            "QLabel { color: white; } "
            "QGroupBox { color: white; } "
            "QGroupBox::title { color: white; }"
        )
        self.setCentralWidget(root)
        main_layout = QHBoxLayout(root)

        left = QVBoxLayout()
        right = QVBoxLayout()
        main_layout.addLayout(left, 1)
        main_layout.addLayout(right, 2)

        # Config button
        self.cfg_btn = QPushButton("Config: (none)")
        self.cfg_btn.clicked.connect(self._open_config_popup)
        left.addWidget(self.cfg_btn)

        # Connections
        conn_group = QGroupBox("Connections")
        conn_group.setMaximumHeight(60)
        conn_layout = QHBoxLayout(conn_group)
        self.conn_buttons = {}
        for name in ("Microfluidics", "Dobot", "Microcontroller"):
            btn = QPushButton(name)
            btn.clicked.connect(lambda _, n=name: self._open_connection_popup(n))
            conn_layout.addWidget(btn)
            self.conn_buttons[name] = btn
        left.addWidget(conn_group)

        # Nozzle status - horizontal layout
        nozzle_frame = QFrame()
        nozzle_layout = QHBoxLayout(nozzle_frame)
        self.nozzle_labels = []
        for i in range(1, 4):
            lbl = QLabel(f"Line {i}: Idle")
            nozzle_layout.addWidget(lbl)
            self.nozzle_labels.append(lbl)
        left.addWidget(nozzle_frame)

        # Composition arrays
        arrays_group = QGroupBox("Composition Arrays (Intake)")
        arrays_main_layout = QVBoxLayout(arrays_group)

        lipid_library_btn = QPushButton("Lipid Library")
        lipid_library_btn.clicked.connect(self._open_lipid_library_popup)
        arrays_main_layout.addWidget(lipid_library_btn)

        buffer_btn = QPushButton("Buffer")
        buffer_btn.clicked.connect(self._open_buffer_popup)
        buffer_btn.setFixedWidth(180)
        buffer_btn.setStyleSheet("background-color: #555555; color: white;")
        self.buffer_btn = buffer_btn
        buffer_row = QHBoxLayout()
        buffer_row.addStretch()
        buffer_row.addWidget(buffer_btn)
        buffer_row.addStretch()
        arrays_main_layout.addLayout(buffer_row)

        arrays_horizontal = QHBoxLayout()
        self.intake_wells = {}
        for plate_idx in range(1, 4):
            section = QVBoxLayout()
            grid = QGridLayout()
            grid.setSpacing(4)
            for r in range(5):
                for c in range(3):
                    well = IntakeWellWidget(plate_idx, r + 1, c + 1, on_click=self._open_lipid_popup)
                    grid.addWidget(well, r, c)
                    self.intake_wells[(plate_idx, r + 1, c + 1)] = well
            section.addLayout(grid)
            arrays_horizontal.addLayout(section)

        arrays_main_layout.addLayout(arrays_horizontal)
        left.addWidget(arrays_group)

        # Clean all button - above plate map
        clean_btn = QPushButton("Clean All")
        clean_btn.clicked.connect(self._clean_all)
        clean_btn.setEnabled(False)
        self.clean_btn = clean_btn
        left.addWidget(clean_btn)

        # Output plate map - below clean all
        plate_group = QGroupBox("Output Plate Map (96-well)")
        plate_layout = QHBoxLayout(plate_group)

        # Plate selector - compact 3x2 grid on left
        plate_selector_layout = QGridLayout()
        plate_selector_layout.setSpacing(0)
        plate_selector_layout.setContentsMargins(0, 0, 0, 0)
        self.plate_buttons = []
        for i in range(1, 7):
            btn = QPushButton(str(i))
            btn.setFixedSize(50, 30)
            btn.setStyleSheet("font-size: 10px;")
            btn.clicked.connect(lambda _, idx=i: self._switch_output_plate(idx))
            row = (i - 1) // 2
            col = (i - 1) % 2
            plate_selector_layout.addWidget(btn, row, col, alignment=Qt.AlignmentFlag.AlignTop)
            self.plate_buttons.append(btn)

        plate_selector_frame = QWidget()
        plate_selector_frame.setLayout(plate_selector_layout)
        plate_selector_frame.setMaximumWidth(110)
        plate_layout.addWidget(plate_selector_frame, alignment=Qt.AlignmentFlag.AlignTop)

        self.output_plate_widget = OutputPlateWidget()
        self.output_plate_widget.on_select = self._on_start_well_selected

        plate_layout.addWidget(self.output_plate_widget, alignment=Qt.AlignmentFlag.AlignTop)

        left.addWidget(plate_group)
        self._update_plate_button_styles()

        # Admin tools button at bottom
        left.addStretch()
        admin_btn = QPushButton("Admin Tools")
        admin_btn.clicked.connect(self._open_admin_tools)
        admin_btn.setStyleSheet("background-color: #444444; font-size: 10px;")
        left.addWidget(admin_btn)

        # Right side
        import_exp_btn = QPushButton("Import Experiments")
        import_exp_btn.clicked.connect(self._import_experiments_csv)
        right.addWidget(import_exp_btn)

        add_exp_btn = QPushButton("Add Experiment")
        add_exp_btn.clicked.connect(self._open_experiment_popup)
        right.addWidget(add_exp_btn)

        exp_group = QGroupBox("Experiments")
        exp_layout = QVBoxLayout(exp_group)
        self.exp_table = QTreeWidget()
        self.exp_table.setHeaderLabels(["Order", "Log #", "Name", "Lipids", "Est Lines", "# Comp", "Status"])
        self.exp_table.setMaximumHeight(400)
        self.exp_table.itemDoubleClicked.connect(self._open_experiment_details)
        self.exp_table.itemSelectionChanged.connect(self._update_experiment_queue_buttons)
        exp_layout.addWidget(self.exp_table)

        move_row = QHBoxLayout()
        self.move_up_btn = QPushButton("Move Up")
        self.move_down_btn = QPushButton("Move Down")
        self.delete_exp_btn = QPushButton("Delete")
        self.delete_exp_btn.setStyleSheet(
            "QPushButton { background-color: #b54a4a; color: white; }"
            "QPushButton:disabled { background-color: #666666; color: #BBBBBB; }"
        )
        self.move_up_btn.clicked.connect(lambda: self._move_selected_experiment(-1))
        self.move_down_btn.clicked.connect(lambda: self._move_selected_experiment(1))
        self.delete_exp_btn.clicked.connect(self._delete_selected_experiment)
        move_row.addWidget(self.move_up_btn)
        move_row.addWidget(self.move_down_btn)
        move_row.addWidget(self.delete_exp_btn)
        exp_layout.addLayout(move_row)
        self._update_experiment_queue_buttons()

        # Lipid stocks in use
        lipid_info_layout = QHBoxLayout()
        self.lipid_stock_labels = []
        for i in range(3):
            frame = QVBoxLayout()
            name_lbl = QLabel(f"Lipid {i+1}: -")
            vol_lbl = QLabel("Vol: -")
            frame.addWidget(name_lbl)
            frame.addWidget(vol_lbl)
            lipid_info_layout.addLayout(frame)
            self.lipid_stock_labels.append((name_lbl, vol_lbl))
        exp_layout.addLayout(lipid_info_layout)

        right.addWidget(exp_group)

        # Status
        self.status_label = QLabel("Status: Idle")
        self.status_label.setFrameStyle(QLabel.Shape.Panel | QLabel.Shadow.Sunken)
        right.addWidget(self.status_label)
        self.line_status_label = QLabel("L1: Idle | L2: Idle | L3: Idle")
        self.line_status_label.setFrameStyle(QLabel.Shape.Panel | QLabel.Shadow.Sunken)
        self.line_status_label.setStyleSheet("font-family: Consolas, monospace; color: #D0D0D0;")
        right.addWidget(self.line_status_label)

        # Start well indicator
        self.start_well_label = QLabel("Start Well: P1 A1")
        self.start_well_label.setFrameStyle(QLabel.Shape.Panel | QLabel.Shadow.Sunken)
        self.start_well_label.setStyleSheet("font-size: 10px; color: #CCCCCC;")
        right.addWidget(self.start_well_label)
        self.start_well_mode_label = QLabel("Plate Click Mode: Global (no experiment selected)")
        self.start_well_mode_label.setStyleSheet("font-size: 10px; color: #AAAAAA;")
        right.addWidget(self.start_well_mode_label)
        self._selected_start_exp_id = None

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        right.addWidget(self.progress_bar)

        # Plots (pyqtgraph)
        plots_group = QGroupBox("Live Data")
        plots_layout = QVBoxLayout(plots_group)
        self.live_detail_label = QLabel("Run: -")
        self.live_detail_label.setWordWrap(True)
        self.live_detail_label.setStyleSheet("font-size: 10px; color: #DDDDDD;")
        plots_layout.addWidget(self.live_detail_label)
        flow_focus_row = QHBoxLayout()
        flow_focus_row.addWidget(QLabel("Flow Y-Range:"))
        self.flow_y_range_mode_combo = QComboBox()
        self.flow_y_range_mode_combo.addItem("Lipids Only", userData="lipids")
        self.flow_y_range_mode_combo.addItem("Whole (Incl. Buffer)", userData="all")
        self.flow_y_range_mode_combo.setCurrentIndex(0)
        flow_focus_row.addWidget(self.flow_y_range_mode_combo)
        flow_focus_row.addStretch(1)
        plots_layout.addLayout(flow_focus_row)

        self.flow_plot = pg.PlotWidget(title="Flow Rates (µL/min)")
        self.flow_plot.setBackground("#2B2B2B")
        self.flow_plot.showGrid(x=True, y=True, alpha=0.3)
        self.flow_plot.setLabel("left", "Flow", units="µL/min")
        self.flow_plot.setLabel("bottom", "Time", units="s")
        self.flow_plot.setMaximumHeight(200)
        self.flow_curves = []
        colors = [(0, 255, 255), (255, 165, 0), (255, 0, 0), (138, 43, 226)]
        self._default_plot_colors = colors
        for i in range(4):
            curve = self.flow_plot.plot(pen=pg.mkPen(color=colors[i], width=2))
            self.flow_curves.append(curve)
        extra_color = (80, 220, 140)
        self.extra_flow_curve = self.flow_plot.plot(
            pen=pg.mkPen(color=extra_color, width=2, style=Qt.PenStyle.DotLine)
        )
        plots_layout.addWidget(self.flow_plot)

        # Collection markers (dotted vertical lines)
        self.collection_lines_flow = []
        self.collection_lines_pressure = []
        self._last_collection_markers = []

        self.pressure_plot = pg.PlotWidget(title="Pressures (mbar)")
        self.pressure_plot.setBackground("#2B2B2B")
        self.pressure_plot.showGrid(x=True, y=True, alpha=0.3)
        self.pressure_plot.setLabel("left", "Pressure", units="mbar")
        self.pressure_plot.setLabel("bottom", "Time", units="s")
        self.pressure_plot.setMaximumHeight(200)
        self.pressure_curves_set = []
        self.pressure_curves_act = []
        for i in range(4):
            curve_set = self.pressure_plot.plot(pen=pg.mkPen(color=colors[i], width=2, style=Qt.PenStyle.SolidLine))
            curve_act = self.pressure_plot.plot(pen=pg.mkPen(color=colors[i], width=2, style=Qt.PenStyle.DashLine))
            self.pressure_curves_set.append(curve_set)
            self.pressure_curves_act.append(curve_act)
        self.extra_pressure_curve_set = self.pressure_plot.plot(
            pen=pg.mkPen(color=extra_color, width=2, style=Qt.PenStyle.DotLine)
        )
        self.extra_pressure_curve_act = self.pressure_plot.plot(
            pen=pg.mkPen(color=extra_color, width=2, style=Qt.PenStyle.DashLine)
        )
        plots_layout.addWidget(self.pressure_plot)
        self.flow_values_label = QLabel("Ch1: - | Ch2: - | Ch3: - | Ch4: -")
        self.flow_values_label.setStyleSheet("font-size: 10px; color: #CFCFCF;")
        plots_layout.addWidget(self.flow_values_label)

        right.addWidget(plots_group)

        # Controls
        controls = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._start)
        self.start_btn.setEnabled(False)
        controls.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        controls.addWidget(self.stop_btn)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self._pause)
        self.pause_btn.setEnabled(False)
        controls.addWidget(self.pause_btn)

        self.skip_btn = QPushButton("Skip")
        self.skip_btn.clicked.connect(self._skip)
        self.skip_btn.setEnabled(False)
        controls.addWidget(self.skip_btn)

        right.addLayout(controls)

    # --- UI helpers ---
    def _toggle_connection(self, name):
        connected = self.control_api.toggle_connection(name)
        btn = self.conn_buttons[name]
        btn.setStyleSheet("background-color: #7CFC90;" if connected else "")

    def _open_config_popup_legacy(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Configuration")
        layout = QVBoxLayout(dlg)

        # Preset controls
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        preset_combo = QComboBox()
        preset_combo.setEditable(False)
        preset_combo.addItems(self.control_api.list_config_presets())
        preset_row.addWidget(preset_combo)
        preset_row.addWidget(QLabel("Name:"))
        preset_name = QLineEdit()
        preset_name.setPlaceholderText("preset name")
        preset_row.addWidget(preset_name)
        save_preset_btn = QPushButton("Save Preset")
        load_preset_btn = QPushButton("Load Preset")
        delete_preset_btn = QPushButton("Delete Preset")
        preset_row.addWidget(save_preset_btn)
        preset_row.addWidget(load_preset_btn)
        preset_row.addWidget(delete_preset_btn)
        layout.addLayout(preset_row)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Parameter", "Value", "Notes"])
        layout.addWidget(table)

        defaults = [
            ("StartFlush", True, "Flush at start of run"),
            ("ZeroFlush", False, "Flush after zero flow condition"),
            ("StartFlushRampEnabled", True, "Enable FR1->FR2 ramping for start flushes"),
            ("ZeroFlushRampEnabled", True, "Enable FR1->FR2 ramping for zero flushes"),
            ("FlushFRs", "[420,4,4,4]", "Buffer + 3 lipid flush FRs"),
            ("StartFlush1FRs", "[420,4,4,4]", "Start flush setpoint 1 FRs (Buffer + 3 lipid)"),
            ("StartFlush2FRs", "[420,4,4,4]", "Start flush setpoint 2 FRs (ramp target)"),
            ("prime_to_startflush_ramp_s", 60.0, "Prime->StartFlush1 ramp duration (s)"),
            ("prime_buffer_fr", 100.0, "Prime run buffer flow rate (uL/min)"),
            ("prime_lipid_fr", 20.0, "Prime run lipid line flow rate (uL/min per active line)"),
            ("prime_rna_buffer_fr", 20.0, "Prime run RNA buffer flow rate for line 3 constant mode (uL/min)"),
            ("rna_buffer_startflush_fr", 0.0, "RNA buffer start-flush flow rate (uL/min, 0=fallback to run)"),
            ("rna_buffer_zeroflush_fr", 0.0, "RNA buffer zero-flush flow rate (uL/min, 0=fallback to run)"),
            ("prime_all", False, "Prime all active experiment lines at start, not only newly loaded lines"),
            ("ActiveLines", "[1,2,3]", "Physical lipid lines to use for loading/runs"),
            ("line3_RNA_constant", False, "Enable experiment option: line 3 as constant-flow RNA buffer"),
            ("Remove Stoppers", False, "Before each load, remove stopper from selected intake well"),
            ("ZeroFlowBlocking", True, "Zero-flow: close to chip, set pressure 0, skip flow tracking"),
            ("dynamic_line_remap", False, "Reorder lipid slots at runtime to reuse currently loaded lines"),
            ("EquilibrationRetry", False, "If composition fails to equilibrate, run a flush and retry same well once"),
            ("flush_time_s", 0.0, "Extra hold after stabilization for flush compositions (s)"),
            ("first_comp_delay_s", 0.0, "Extra stabilization time added to first composition (s)"),
            ("zero_block_hold_s", 3.0, "Zero-block hold time after valve close/release sequencing (s)"),
            ("priming_hold_time", 30.0, "Priming hold time after equilibrium (s)"),
            ("stable_flush_time_s", 30.0, "Air flush stable time (s)"),
            ("cleaning_flush_pressure_mbar", 70.0, "Air flush pressure used during cleaning sequences (mbar)"),
            ("wash_cycles", 1, "Number of wash+post-flush cycles during cleaning"),
            ("stable_load_time_s", 6.5, "Load detection stable time (s)"),
            ("load_flush_through_chip", False, "During loading pre-flush, run through chip path"),
            ("period", 0.5, "PID loop period (s)"),
            ("K_p", "[0.5,500,500,500]", "PID proportional gains"),
            ("K_i", 0.001, "PID integral gain"),
            ("p_incr", "[-100,100]", "Pressure increment clamp"),
            ("p_range", "[0,2000]", "Pressure limits (mbar)"),
            ("max_equilibration_t", 180, "Max equilibration time (s)"),
            ("maxfrerror", "[100,0.2]", "Max FR error [buffer, lipid]"),
            ("expul_t", 13.4, "Stable-flow hold time before collection begins (s)"),
            ("sensor1", "[1,5,1,0]", "Channel 1 sensor config"),
            ("sensor2", "[2,2,1,0]", "Channel 2 sensor config"),
            ("sensor3", "[3,2,1,0]", "Channel 3 sensor config"),
            ("sensor4", "[4,2,1,0]", "Channel 4 sensor config"),
            ("sensorcorr", "[[0,0,0,1.0897,-1.2766],[0.2673,-0.8813,1.3205,1.1869,-0.2],[0.2673,-0.8813,1.3205,1.1869,-0.2],[0.2673,-0.8813,1.3205,1.1869,-0.2]]", "Sensor corrections"),
            ("min_nonzero_set_fr", 0.25, "Setpoint floor for zero flow rates (µL/min)"),
        ]

        cfg = self.control_api.get_config()
        table.setRowCount(len(defaults))
        for i, (p, v, note) in enumerate(defaults):
            table.setItem(i, 0, QTableItem(str(p)))
            current = cfg.get(p, v)
            if isinstance(v, bool):
                cb = QComboBox()
                cb.addItems(["True", "False"])
                cb.setCurrentText("True" if bool(current) else "False")
                table.setCellWidget(i, 1, cb)
            else:
                table.setItem(i, 1, QTableItem(str(current)))
            table.setItem(i, 2, QTableItem(note))

        def _save():
            new_cfg = {}
            for i in range(table.rowCount()):
                key = table.item(i, 0).text()
                widget = table.cellWidget(i, 1)
                if isinstance(widget, QComboBox):
                    new_cfg[key] = widget.currentText() == "True"
                else:
                    new_cfg[key] = table.item(i, 1).text()
            self.control_api.set_config(new_cfg)
            dlg.accept()

        def _refresh_presets():
            preset_combo.clear()
            preset_combo.addItems(self.control_api.list_config_presets())

        def _save_preset():
            name = preset_name.text().strip()
            if not name:
                QMessageBox.warning(dlg, "Name Required", "Enter a preset name to save.")
                return
            if name in self.control_api.list_config_presets():
                resp = QMessageBox.question(
                    dlg,
                    "Overwrite Preset",
                    f"Preset '{name}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    return
            # Save current table values as the preset
            new_cfg = {}
            for i in range(table.rowCount()):
                key = table.item(i, 0).text()
                widget = table.cellWidget(i, 1)
                if isinstance(widget, QComboBox):
                    new_cfg[key] = widget.currentText() == "True"
                else:
                    new_cfg[key] = table.item(i, 1).text()
            self.control_api.set_config(new_cfg)
            self.control_api.save_config_preset(name)
            _refresh_presets()
            preset_combo.setCurrentText(name)
            QMessageBox.information(dlg, "Saved", f"Preset '{name}' saved.")

        def _load_preset():
            name = preset_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Select Preset", "Select a preset to load.")
                return
            cfg_preset = self.control_api.load_config_preset(name)
            if not cfg_preset:
                QMessageBox.warning(dlg, "Not Found", f"Preset '{name}' not found.")
                return
            # Apply preset values to table
            for i in range(table.rowCount()):
                key = table.item(i, 0).text()
                if key in cfg_preset:
                    widget = table.cellWidget(i, 1)
                    if isinstance(widget, QComboBox):
                        widget.setCurrentText("True" if bool(cfg_preset[key]) else "False")
                    else:
                        table.item(i, 1).setText(str(cfg_preset[key]))
            self.control_api.set_config(cfg_preset)
            QMessageBox.information(dlg, "Loaded", f"Preset '{name}' loaded.")

        def _delete_preset():
            name = preset_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Select Preset", "Select a preset to delete.")
                return
            resp = QMessageBox.question(
                dlg,
                "Delete Preset",
                f"Delete preset '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
            self.control_api.delete_config_preset(name)
            _refresh_presets()
            QMessageBox.information(dlg, "Deleted", f"Preset '{name}' deleted.")

        save_preset_btn.clicked.connect(_save_preset)
        load_preset_btn.clicked.connect(_load_preset)
        delete_preset_btn.clicked.connect(_delete_preset)

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)
        dlg.exec()

    def _config_defaults(self):
        return [
            ("StartFlush", True, "Flush at start of run"),
            ("ZeroFlush", False, "Flush after zero flow condition"),
            ("StartFlushRampEnabled", True, "Enable FR1->FR2 ramping for start flushes"),
            ("ZeroFlushRampEnabled", True, "Enable FR1->FR2 ramping for zero flushes"),
            ("FlushFRs", "[420,4,4,4]", "Buffer + 3 lipid flush FRs"),
            ("StartFlush1FRs", "[420,4,4,4]", "Start flush setpoint 1 FRs (Buffer + 3 lipid)"),
            ("StartFlush2FRs", "[420,4,4,4]", "Start flush setpoint 2 FRs (ramp target)"),
            ("prime_to_startflush_ramp_s", 60.0, "Prime->StartFlush1 ramp duration (s)"),
            ("prime_buffer_fr", 100.0, "Prime run buffer flow rate (uL/min)"),
            ("prime_lipid_fr", 20.0, "Prime run lipid line flow rate (uL/min per active line)"),
            ("prime_rna_buffer_fr", 20.0, "Prime run RNA buffer flow rate for line 3 constant mode (uL/min)"),
            ("rna_buffer_startflush_fr", 0.0, "RNA buffer start-flush flow rate (uL/min, 0=fallback to run)"),
            ("rna_buffer_zeroflush_fr", 0.0, "RNA buffer zero-flush flow rate (uL/min, 0=fallback to run)"),
            ("prime_all", False, "Prime all active experiment lines at start, not only newly loaded lines"),
            ("ActiveLines", "[1,2,3]", "Physical lipid lines to use for loading/runs"),
            ("line3_RNA_constant", False, "Enable experiment option: line 3 as constant-flow RNA buffer"),
            ("Remove Stoppers", False, "Before each load, remove stopper from selected intake well"),
            ("ZeroFlowBlocking", True, "Zero-flow: close to chip, set pressure 0, skip flow tracking"),
            ("dynamic_line_remap", False, "Reorder lipid slots at runtime to reuse currently loaded lines"),
            ("EquilibrationRetry", False, "If composition fails to equilibrate, run a flush and retry same well once"),
            ("flush_time_s", 0.0, "Extra hold after stabilization for flush compositions (s)"),
            ("first_comp_delay_s", 0.0, "Extra stabilization time added to first composition (s)"),
            ("zero_block_hold_s", 3.0, "Zero-block hold time after valve close/release sequencing (s)"),
            ("priming_hold_time", 30.0, "Priming hold time after equilibrium (s)"),
            ("stable_flush_time_s", 30.0, "Air flush stable time (s)"),
            ("cleaning_flush_pressure_mbar", 70.0, "Air flush pressure used during cleaning sequences (mbar)"),
            ("wash_cycles", 1, "Number of wash+post-flush cycles during cleaning"),
            ("stable_load_time_s", 6.5, "Load detection stable time (s)"),
            ("load_flush_through_chip", False, "During loading pre-flush, run through chip path"),
            ("period", 0.5, "PID loop period (s)"),
            ("K_p", "[0.5,500,500,500]", "PID proportional gains"),
            ("K_i", 0.001, "PID integral gain"),
            ("p_incr", "[-100,100]", "Pressure increment clamp"),
            ("p_range", "[0,2000]", "Pressure limits (mbar)"),
            ("max_equilibration_t", 180, "Max equilibration time (s)"),
            ("maxfrerror", "[100,0.2]", "Max FR error [buffer, lipid]"),
            ("expul_t", 13.4, "Stable-flow hold time before collection begins (s)"),
            ("sensor1", "[1,5,1,0]", "Channel 1 sensor config"),
            ("sensor2", "[2,2,1,0]", "Channel 2 sensor config"),
            ("sensor3", "[3,2,1,0]", "Channel 3 sensor config"),
            ("sensor4", "[4,2,1,0]", "Channel 4 sensor config"),
            ("sensor_rna", "[4,2,1,0]", "RNA mode sensor config for channel 4"),
            ("sensorcorr", "[[0,0,0,1.0897,-1.2766],[0.2673,-0.8813,1.3205,1.1869,-0.2],[0.2673,-0.8813,1.3205,1.1869,-0.2],[0.2673,-0.8813,1.3205,1.1869,-0.2]]", "Sensor corrections"),
            ("sensorcorr_rna", "[0.2673,-0.8813,1.3205,1.1869,-0.2]", "RNA mode sensor correction for channel 4"),
            ("min_nonzero_set_fr", 0.25, "Setpoint floor for zero flow rates (uL/min)"),
        ]

    def _fill_config_table(self, table: QTableWidget, cfg: dict):
        defaults = self._config_defaults()
        rna_keys = {
            "prime_rna_buffer_fr",
            "rna_buffer_startflush_fr",
            "rna_buffer_zeroflush_fr",
        }
        cfg_map = (cfg or {})
        rna_mode_enabled = bool(cfg_map.get("line3_RNA_constant", cfg_map.get("line3_constant_mode_enabled", False)))
        if not rna_mode_enabled:
            defaults = [row for row in defaults if row[0] not in rna_keys]
        table.setRowCount(len(defaults))
        for i, (param, default_val, note) in enumerate(defaults):
            table.setItem(i, 0, QTableItem(str(param)))
            current = cfg.get(param, default_val)
            if isinstance(default_val, bool):
                cb = QComboBox()
                cb.addItems(["True", "False"])
                cb.setCurrentText("True" if bool(current) else "False")
                table.setCellWidget(i, 1, cb)
            else:
                table.setItem(i, 1, QTableItem(str(current)))
            table.setItem(i, 2, QTableItem(note))

    def _read_config_table(self, table: QTableWidget) -> dict:
        cfg = {}
        for i in range(table.rowCount()):
            key_item = table.item(i, 0)
            if key_item is None:
                continue
            key = key_item.text()
            widget = table.cellWidget(i, 1)
            if isinstance(widget, QComboBox):
                cfg[key] = widget.currentText() == "True"
            else:
                val_item = table.item(i, 1)
                cfg[key] = val_item.text() if val_item is not None else ""
        return cfg

    def _refresh_selected_config_label(self):
        try:
            name = str(self.control_api.get_selected_config_preset() or "").strip()
        except Exception:
            name = ""
        if hasattr(self, "cfg_btn") and self.cfg_btn is not None:
            self.cfg_btn.setText(f"Config: {name if name else '(none)'}")

    def _open_config_editor_dialog(self, preset_name: str = "", source_cfg: dict = None):
        import ast

        dlg = QDialog(self)
        dlg.setWindowTitle("Config Editor")
        dlg.setMinimumSize(1100, 760)
        layout = QVBoxLayout(dlg)

        row = QHBoxLayout()
        row.addWidget(QLabel("Config Name:"))
        name_edit = QLineEdit(str(preset_name or ""))
        name_edit.setPlaceholderText("enter config name")
        row.addWidget(name_edit)
        layout.addLayout(row)

        cfg_src = dict(source_cfg or self.control_api.get_config() or {})

        def _to_bool(v, default=False):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            if v is None:
                return default
            return bool(v)

        def _to_float(v, default=0.0):
            try:
                return float(v)
            except Exception:
                return float(default)

        def _to_int(v, default=0):
            try:
                return int(float(v))
            except Exception:
                return int(default)

        def _to_list(v, default):
            if isinstance(v, str):
                try:
                    v = ast.literal_eval(v)
                except Exception:
                    return list(default)
            if not isinstance(v, (list, tuple)):
                return list(default)
            return list(v)

        def _mk_check(key, default=False):
            cb = QCheckBox()
            cb.setChecked(_to_bool(cfg_src.get(key, default), default))
            return cb

        def _mk_float(key, default=0.0, min_v=-1e9, max_v=1e9, dec=3):
            sp = QDoubleSpinBox()
            sp.setDecimals(dec)
            sp.setRange(min_v, max_v)
            sp.setValue(_to_float(cfg_src.get(key, default), default))
            return sp

        def _mk_int(key, default=0, min_v=-100000, max_v=100000):
            sp = QSpinBox()
            sp.setRange(min_v, max_v)
            sp.setValue(_to_int(cfg_src.get(key, default), default))
            return sp

        def _mk_array_spinboxes(key, default_vals, is_int=False, dec=3, min_v=-1e9, max_v=1e9):
            vals = _to_list(cfg_src.get(key, default_vals), default_vals)
            out = []
            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(6)
            for i, d in enumerate(default_vals):
                if is_int:
                    sp = QSpinBox()
                    sp.setRange(int(min_v), int(max_v))
                    sp.setValue(_to_int(vals[i] if i < len(vals) else d, d))
                else:
                    sp = QDoubleSpinBox()
                    sp.setDecimals(dec)
                    sp.setRange(min_v, max_v)
                    sp.setValue(_to_float(vals[i] if i < len(vals) else d, d))
                sp.setMaximumWidth(120)
                row_l.addWidget(sp)
                out.append(sp)
            row_l.addStretch(1)
            return row_w, out

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # Flush
        flush_tab = QWidget()
        flush_form = QFormLayout(flush_tab)
        w_StartFlush = _mk_check("StartFlush", True)
        w_ZeroFlush = _mk_check("ZeroFlush", False)
        w_StartFlushRampEnabled = _mk_check("StartFlushRampEnabled", True)
        w_ZeroFlushRampEnabled = _mk_check("ZeroFlushRampEnabled", True)
        w_flush_time_s = _mk_float("flush_time_s", 0.0, 0.0, 3600.0, 2)
        w_rna_buffer_startflush_fr = _mk_float("rna_buffer_startflush_fr", 0.0, 0.0, 10000.0, 3)
        w_rna_buffer_zeroflush_fr = _mk_float("rna_buffer_zeroflush_fr", 0.0, 0.0, 10000.0, 3)
        w_FlushFRs_row, w_FlushFRs = _mk_array_spinboxes("FlushFRs", [420, 4, 4, 4], is_int=False, dec=3, min_v=0.0, max_v=10000.0)
        w_StartFlush1FRs_row, w_StartFlush1FRs = _mk_array_spinboxes("StartFlush1FRs", [420, 4, 4, 4], is_int=False, dec=3, min_v=0.0, max_v=10000.0)
        w_StartFlush2FRs_row, w_StartFlush2FRs = _mk_array_spinboxes("StartFlush2FRs", [420, 4, 4, 4], is_int=False, dec=3, min_v=0.0, max_v=10000.0)
        flush_form.addRow("Start Flush", w_StartFlush)
        flush_form.addRow("Zero Flush", w_ZeroFlush)
        flush_form.addRow("Start Flush Ramp", w_StartFlushRampEnabled)
        flush_form.addRow("Zero Flush Ramp", w_ZeroFlushRampEnabled)
        flush_form.addRow("Flush FRs [Buffer,L1,L2,L3]", w_FlushFRs_row)
        flush_form.addRow("Start Flush 1 FRs", w_StartFlush1FRs_row)
        flush_form.addRow("Start Flush 2 FRs", w_StartFlush2FRs_row)
        flush_form.addRow("RNA Start Flush FR (uL/min)", w_rna_buffer_startflush_fr)
        flush_form.addRow("RNA Zero Flush FR (uL/min)", w_rna_buffer_zeroflush_fr)
        flush_form.addRow("Flush Hold Time (s)", w_flush_time_s)
        tabs.addTab(flush_tab, "Flush")

        # Prime
        prime_tab = QWidget()
        prime_form = QFormLayout(prime_tab)
        w_prime_to_startflush_ramp_s = _mk_float("prime_to_startflush_ramp_s", 60.0, 0.0, 3600.0, 2)
        w_prime_buffer_fr = _mk_float("prime_buffer_fr", 100.0, 0.0, 10000.0, 3)
        w_prime_lipid_fr = _mk_float("prime_lipid_fr", 20.0, 0.0, 10000.0, 3)
        w_prime_rna_buffer_fr = _mk_float("prime_rna_buffer_fr", 20.0, 0.0, 10000.0, 3)
        w_prime_all = _mk_check("prime_all", False)
        w_priming_hold_time = _mk_float("priming_hold_time", 30.0, 0.0, 3600.0, 2)
        prime_form.addRow("Prime->StartFlush Ramp (s)", w_prime_to_startflush_ramp_s)
        prime_form.addRow("Prime Buffer FR (uL/min)", w_prime_buffer_fr)
        prime_form.addRow("Prime Lipid FR (uL/min)", w_prime_lipid_fr)
        prime_form.addRow("Prime RNA Buffer FR (uL/min)", w_prime_rna_buffer_fr)
        prime_form.addRow("Prime All Active Lines", w_prime_all)
        prime_form.addRow("Priming Hold Time (s)", w_priming_hold_time)
        tabs.addTab(prime_tab, "Prime")

        # Control
        control_tab = QWidget()
        control_form = QFormLayout(control_tab)
        active_lines_default = cfg_src.get("ActiveLines", [1, 2, 3])
        w_ActiveLines = QLineEdit(str(active_lines_default))
        w_line3_RNA_constant = _mk_check("line3_RNA_constant", False)
        w_RemoveStoppers = _mk_check("Remove Stoppers", False)
        w_ZeroFlowBlocking = _mk_check("ZeroFlowBlocking", True)
        w_dynamic_line_remap = _mk_check("dynamic_line_remap", False)
        control_form.addRow("Active Lines (e.g. [1,2,3])", w_ActiveLines)
        control_form.addRow("Enable Line3 RNA Constant Mode", w_line3_RNA_constant)
        control_form.addRow("Remove Stoppers", w_RemoveStoppers)
        control_form.addRow("Zero Flow Blocking", w_ZeroFlowBlocking)
        control_form.addRow("Dynamic Line Remap", w_dynamic_line_remap)
        tabs.addTab(control_tab, "Control")

        # Cleaning and Load
        clean_tab = QWidget()
        clean_form = QFormLayout(clean_tab)
        w_wash_cycles = _mk_int("wash_cycles", 1, 1, 100)
        w_stable_flush_time_s = _mk_float("stable_flush_time_s", 30.0, 0.0, 3600.0, 2)
        w_cleaning_flush_pressure_mbar = _mk_float("cleaning_flush_pressure_mbar", 70.0, 0.0, 2000.0, 1)
        w_stable_load_time_s = _mk_float("stable_load_time_s", 6.5, 0.0, 600.0, 2)
        w_load_flush_through_chip = _mk_check("load_flush_through_chip", False)
        clean_form.addRow("Wash Cycles", w_wash_cycles)
        clean_form.addRow("Stable Flush Time (s)", w_stable_flush_time_s)
        clean_form.addRow("Cleaning Flush Pressure (mbar)", w_cleaning_flush_pressure_mbar)
        clean_form.addRow("Stable Load Time (s)", w_stable_load_time_s)
        clean_form.addRow("Load Flush Through Chip", w_load_flush_through_chip)
        tabs.addTab(clean_tab, "Cleaning and Load")

        # Timings
        timings_tab = QWidget()
        timings_form = QFormLayout(timings_tab)
        w_first_comp_delay_s = _mk_float("first_comp_delay_s", 0.0, 0.0, 3600.0, 2)
        w_zero_block_hold_s = _mk_float("zero_block_hold_s", 3.0, 0.0, 3600.0, 2)
        timings_form.addRow("First Composition Delay (s)", w_first_comp_delay_s)
        timings_form.addRow("Zero Block Hold (s)", w_zero_block_hold_s)
        tabs.addTab(timings_tab, "Timings")

        # PID
        pid_tab = QWidget()
        pid_form = QFormLayout(pid_tab)
        w_period = _mk_float("period", 0.5, 0.01, 60.0, 3)
        w_K_p_row, w_K_p = _mk_array_spinboxes("K_p", [0.5, 500, 500, 500], is_int=False, dec=4, min_v=0.0, max_v=1e6)
        w_K_i = _mk_float("K_i", 0.001, 0.0, 1e6, 6)
        w_p_incr_row, w_p_incr = _mk_array_spinboxes("p_incr", [-100, 100], is_int=False, dec=3, min_v=-1e6, max_v=1e6)
        w_p_range_row, w_p_range = _mk_array_spinboxes("p_range", [0, 2000], is_int=False, dec=3, min_v=-1e6, max_v=1e6)
        w_max_equilibration_t = _mk_float("max_equilibration_t", 180, 0.0, 36000.0, 2)
        w_maxfrerror_row, w_maxfrerror = _mk_array_spinboxes("maxfrerror", [100, 0.2], is_int=False, dec=4, min_v=0.0, max_v=1e6)
        w_expul_t = _mk_float("expul_t", 13.4, 0.0, 3600.0, 2)
        w_EquilibrationRetry = _mk_check("EquilibrationRetry", False)
        pid_form.addRow("Period (s)", w_period)
        pid_form.addRow("Kp [Buffer,L1,L2,L3]", w_K_p_row)
        pid_form.addRow("Ki", w_K_i)
        pid_form.addRow("Pressure Increment Clamp [min,max]", w_p_incr_row)
        pid_form.addRow("Pressure Range [min,max] (mbar)", w_p_range_row)
        pid_form.addRow("Max Equilibration Time (s)", w_max_equilibration_t)
        pid_form.addRow("Max FR Error [buffer,lipid]", w_maxfrerror_row)
        pid_form.addRow("Stable-Flow Hold Before Collection (s)", w_expul_t)
        pid_form.addRow("Equilibration Retry", w_EquilibrationRetry)
        tabs.addTab(pid_tab, "PID")

        # Sensors
        sensors_tab = QWidget()
        sensors_layout = QVBoxLayout(sensors_tab)
        sensors_layout.setSpacing(8)

        sensor_cfg_group = QGroupBox("Sensor Configuration [channel, type, unit, reserved]")
        sensor_cfg_grid = QGridLayout(sensor_cfg_group)
        sensor_cfg_grid.addWidget(QLabel("Sensor"), 0, 0)
        sensor_cfg_grid.addWidget(QLabel("C"), 0, 1)
        sensor_cfg_grid.addWidget(QLabel("Type"), 0, 2)
        sensor_cfg_grid.addWidget(QLabel("Unit"), 0, 3)
        sensor_cfg_grid.addWidget(QLabel("R"), 0, 4)

        sensor_cfg_widgets = {}
        for row_idx, key in enumerate(("sensor1", "sensor2", "sensor3", "sensor4", "sensor_rna"), start=1):
            defaults = [row_idx, 2, 1, 0] if key != "sensor1" else [1, 5, 1, 0]
            if key == "sensor_rna":
                defaults = [4, 2, 1, 0]
            vals = _to_list(cfg_src.get(key, defaults), defaults)
            sensor_cfg_grid.addWidget(QLabel(key), row_idx, 0)
            arr = []
            for j in range(4):
                sp = QSpinBox()
                sp.setRange(-9999, 9999)
                sp.setValue(_to_int(vals[j] if j < len(vals) else defaults[j], defaults[j]))
                sp.setMaximumWidth(90)
                sensor_cfg_grid.addWidget(sp, row_idx, j + 1)
                arr.append(sp)
            sensor_cfg_widgets[key] = arr

        sensor_corr_group = QGroupBox("Sensor Calibration Coefficients [a,b,c,d,e]")
        sensor_corr_grid = QGridLayout(sensor_corr_group)
        sensor_corr_grid.addWidget(QLabel("Sensor"), 0, 0)
        for j, lbl in enumerate(("a", "b", "c", "d", "e"), start=1):
            sensor_corr_grid.addWidget(QLabel(lbl), 0, j)

        sensorcorr_default = [
            [0, 0, 0, 1.0897, -1.2766],
            [0.2673, -0.8813, 1.3205, 1.1869, -0.2],
            [0.2673, -0.8813, 1.3205, 1.1869, -0.2],
            [0.2673, -0.8813, 1.3205, 1.1869, -0.2],
        ]
        sensorcorr_val = _to_list(cfg_src.get("sensorcorr", sensorcorr_default), sensorcorr_default)
        if len(sensorcorr_val) < 4:
            sensorcorr_val = sensorcorr_default
        sensorcorr_rna_default = [0.2673, -0.8813, 1.3205, 1.1869, -0.2]
        sensorcorr_rna_val = _to_list(cfg_src.get("sensorcorr_rna", sensorcorr_rna_default), sensorcorr_rna_default)

        sensor_corr_widgets = {}
        corr_keys = ("sensor1_corr", "sensor2_corr", "sensor3_corr", "sensor4_corr", "sensor_rna_corr")
        for row_idx, key in enumerate(corr_keys, start=1):
            sensor_corr_grid.addWidget(QLabel(key), row_idx, 0)
            arr = []
            if key == "sensor_rna_corr":
                vals = sensorcorr_rna_val
            else:
                idx = row_idx - 1
                vals = sensorcorr_val[idx] if idx < len(sensorcorr_val) else sensorcorr_default[idx]
            for j in range(5):
                sp = QDoubleSpinBox()
                sp.setDecimals(6)
                sp.setRange(-1e9, 1e9)
                sp.setValue(_to_float(vals[j] if j < len(vals) else 0.0, 0.0))
                sp.setMaximumWidth(110)
                sensor_corr_grid.addWidget(sp, row_idx, j + 1)
                arr.append(sp)
            sensor_corr_widgets[key] = arr

        w_min_nonzero_set_fr = _mk_float("min_nonzero_set_fr", 0.25, 0.0, 1000.0, 4)
        min_row = QHBoxLayout()
        min_row.addWidget(QLabel("Min Non-Zero Set FR (uL/min):"))
        min_row.addWidget(w_min_nonzero_set_fr)
        min_row.addStretch(1)

        sensors_layout.addWidget(sensor_cfg_group)
        sensors_layout.addWidget(sensor_corr_group)
        sensors_layout.addLayout(min_row)
        sensors_layout.addStretch(1)
        tabs.addTab(sensors_tab, "Sensors")

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        def _save():
            name = name_edit.text().strip()
            if not name:
                QMessageBox.warning(dlg, "Name Required", "Config name is required.")
                return

            cfg_map = dict(cfg_src)
            try:
                # Flush
                cfg_map["StartFlush"] = bool(w_StartFlush.isChecked())
                cfg_map["ZeroFlush"] = bool(w_ZeroFlush.isChecked())
                cfg_map["StartFlushRampEnabled"] = bool(w_StartFlushRampEnabled.isChecked())
                cfg_map["ZeroFlushRampEnabled"] = bool(w_ZeroFlushRampEnabled.isChecked())
                cfg_map["FlushFRs"] = [float(w.value()) for w in w_FlushFRs]
                cfg_map["StartFlush1FRs"] = [float(w.value()) for w in w_StartFlush1FRs]
                cfg_map["StartFlush2FRs"] = [float(w.value()) for w in w_StartFlush2FRs]
                cfg_map["rna_buffer_startflush_fr"] = float(w_rna_buffer_startflush_fr.value())
                cfg_map["rna_buffer_zeroflush_fr"] = float(w_rna_buffer_zeroflush_fr.value())
                cfg_map["flush_time_s"] = float(w_flush_time_s.value())

                # Prime
                cfg_map["prime_to_startflush_ramp_s"] = float(w_prime_to_startflush_ramp_s.value())
                cfg_map["prime_buffer_fr"] = float(w_prime_buffer_fr.value())
                cfg_map["prime_lipid_fr"] = float(w_prime_lipid_fr.value())
                cfg_map["prime_rna_buffer_fr"] = float(w_prime_rna_buffer_fr.value())
                cfg_map["prime_all"] = bool(w_prime_all.isChecked())
                cfg_map["priming_hold_time"] = float(w_priming_hold_time.value())

                # Control
                cfg_map["line3_RNA_constant"] = bool(w_line3_RNA_constant.isChecked())
                cfg_map["Remove Stoppers"] = bool(w_RemoveStoppers.isChecked())
                cfg_map["ZeroFlowBlocking"] = bool(w_ZeroFlowBlocking.isChecked())
                cfg_map["dynamic_line_remap"] = bool(w_dynamic_line_remap.isChecked())
                active_lines_txt = str(w_ActiveLines.text() or "").strip()
                parsed_active_lines = ast.literal_eval(active_lines_txt) if active_lines_txt else [1, 2, 3]
                if not isinstance(parsed_active_lines, (list, tuple)):
                    raise ValueError("ActiveLines must be a list like [1,2,3].")
                cfg_map["ActiveLines"] = [int(x) for x in parsed_active_lines]

                # Cleaning and Load
                cfg_map["wash_cycles"] = int(w_wash_cycles.value())
                cfg_map["stable_flush_time_s"] = float(w_stable_flush_time_s.value())
                cfg_map["cleaning_flush_pressure_mbar"] = float(w_cleaning_flush_pressure_mbar.value())
                cfg_map["stable_load_time_s"] = float(w_stable_load_time_s.value())
                cfg_map["load_flush_through_chip"] = bool(w_load_flush_through_chip.isChecked())

                # Timings
                cfg_map["first_comp_delay_s"] = float(w_first_comp_delay_s.value())
                cfg_map["zero_block_hold_s"] = float(w_zero_block_hold_s.value())

                # PID
                cfg_map["period"] = float(w_period.value())
                cfg_map["K_p"] = [float(w.value()) for w in w_K_p]
                cfg_map["K_i"] = float(w_K_i.value())
                cfg_map["p_incr"] = [float(w.value()) for w in w_p_incr]
                cfg_map["p_range"] = [float(w.value()) for w in w_p_range]
                cfg_map["max_equilibration_t"] = float(w_max_equilibration_t.value())
                cfg_map["maxfrerror"] = [float(w.value()) for w in w_maxfrerror]
                cfg_map["expul_t"] = float(w_expul_t.value())
                cfg_map["EquilibrationRetry"] = bool(w_EquilibrationRetry.isChecked())

                # Sensors
                for key, widgets in sensor_cfg_widgets.items():
                    cfg_map[key] = [int(w.value()) for w in widgets]
                corr_main = []
                for key in ("sensor1_corr", "sensor2_corr", "sensor3_corr", "sensor4_corr"):
                    corr_main.append([float(w.value()) for w in sensor_corr_widgets[key]])
                cfg_map["sensorcorr"] = corr_main
                cfg_map["sensorcorr_rna"] = [float(w.value()) for w in sensor_corr_widgets["sensor_rna_corr"]]
                cfg_map["min_nonzero_set_fr"] = float(w_min_nonzero_set_fr.value())
            except Exception as e:
                QMessageBox.warning(dlg, "Invalid Config", str(e))
                return

            ok, err = self.control_api.create_or_update_named_config(name, cfg_map)
            if not ok:
                QMessageBox.warning(dlg, "Save Failed", err or "Could not save config.")
                return
            self._refresh_selected_config_label()
            dlg.accept()

        save_btn.clicked.connect(_save)
        cancel_btn.clicked.connect(dlg.reject)
        return dlg.exec()

    def _open_config_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Config Selection")
        layout = QVBoxLayout(dlg)

        current_name = str(self.control_api.get_selected_config_preset() or "").strip()
        current_lbl = QLabel(f"Current selected config: {current_name if current_name else '(none)'}")
        current_lbl.setStyleSheet("color: #CCCCCC;")
        layout.addWidget(current_lbl)

        row = QHBoxLayout()
        row.addWidget(QLabel("Saved Configs:"))
        preset_combo = QComboBox()
        preset_combo.setEditable(False)
        row.addWidget(preset_combo)
        layout.addLayout(row)

        btn_row = QHBoxLayout()
        select_btn = QPushButton("Select Existing")
        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        create_btn = QPushButton("Create New")
        close_btn = QPushButton("Close")
        btn_row.addWidget(select_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(create_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        def _refresh_presets():
            names = self.control_api.list_config_presets()
            preset_combo.clear()
            preset_combo.addItems(names)
            selected = str(self.control_api.get_selected_config_preset() or "").strip()
            if selected and selected in names:
                preset_combo.setCurrentText(selected)
            current_lbl.setText(f"Current selected config: {selected if selected else '(none)'}")
            has_any = bool(names)
            select_btn.setEnabled(has_any)
            edit_btn.setEnabled(has_any)
            delete_btn.setEnabled(has_any)

        def _select_existing():
            name = preset_combo.currentText().strip()
            if not name:
                QMessageBox.warning(dlg, "Select Config", "Select a saved config first.")
                return
            ok, err = self.control_api.select_config_preset(name)
            if not ok:
                QMessageBox.warning(dlg, "Select Failed", err or "Could not select config.")
                return
            self._refresh_selected_config_label()
            _refresh_presets()
            QMessageBox.information(dlg, "Selected", f"Selected config '{name}'.")

        def _edit_selected():
            name = preset_combo.currentText().strip()
            if not name:
                QMessageBox.warning(dlg, "Select Config", "Select a saved config first.")
                return
            cfg = self.control_api.load_config_preset(name) or {}
            self._open_config_editor_dialog(name, cfg)
            _refresh_presets()

        def _delete_selected():
            name = preset_combo.currentText().strip()
            if not name:
                QMessageBox.warning(dlg, "Select Config", "Select a saved config first.")
                return
            resp = QMessageBox.question(
                dlg,
                "Delete Config",
                f"Delete config '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
            self.control_api.delete_config_preset(name)
            self._refresh_selected_config_label()
            _refresh_presets()

        def _create_new():
            self._open_config_editor_dialog("", self.control_api.get_config() or {})
            _refresh_presets()

        select_btn.clicked.connect(_select_existing)
        edit_btn.clicked.connect(_edit_selected)
        delete_btn.clicked.connect(_delete_selected)
        create_btn.clicked.connect(_create_new)
        close_btn.clicked.connect(dlg.accept)

        _refresh_presets()
        dlg.exec()

    def _open_lipid_popup(self, plate, row, col):
        well = self.intake_wells[(plate, row, col)]
        if well.lipid_name:
            cfg = self.control_api.load_lipid_config(well.lipid_name) or {}
            conc_mM = cfg.get("concentration_mM", "")
            dlg = QDialog(self)
            dlg.setWindowTitle("Lipid Allocation")
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel(f"Lipid: {well.lipid_name}\nConcentration: {conc_mM} mM"))

            btn_row = QHBoxLayout()
            remove_btn = QPushButton("Remove Allocation")
            change_btn = QPushButton("Change Allocation")
            cancel_btn = QPushButton("Cancel")

            def _remove():
                self.control_api.clear_intake_lipid(plate, row, col)
                self.intake_wells[(plate, row, col)].set_lipid(None, "#555555")
                dlg.accept()

            def _change():
                self.control_api.clear_intake_lipid(plate, row, col)
                self.intake_wells[(plate, row, col)].set_lipid(None, "#555555")
                dlg.accept()
                self._open_lipid_popup(plate, row, col)

            remove_btn.clicked.connect(_remove)
            change_btn.clicked.connect(_change)
            cancel_btn.clicked.connect(dlg.reject)

            btn_row.addWidget(remove_btn)
            btn_row.addWidget(change_btn)
            btn_row.addWidget(cancel_btn)
            layout.addLayout(btn_row)
            dlg.exec()
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Lipid Selection (Plate {plate}, {row},{col})")
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Select existing lipid or create new"))
        combo = QComboBox()
        combo.addItem("Add New Lipid...", userData=None)
        lipid_names = self.control_api.get_lipid_configs()
        for name in lipid_names:
            cfg = self.control_api.load_lipid_config(name) or {}
            conc_mM = cfg.get("concentration_mM", "")
            label = f"{name} ({conc_mM} mM)" if conc_mM != "" else name
            combo.addItem(label, userData=name)
        combo.currentTextChanged.connect(lambda: self._toggle_lipid_inputs(dlg))
        layout.addWidget(combo)

        # New lipid section
        new_lipid_group = QGroupBox("Add New Lipid")
        new_lipid_layout = QVBoxLayout(new_lipid_group)

        new_lipid_layout.addWidget(QLabel("Lipid name"))
        name_input = QLineEdit()
        name_input.setObjectName("lipid_name_input")
        new_lipid_layout.addWidget(name_input)

        new_lipid_layout.addWidget(QLabel("Lipid code (A-Z, 0-9, no spaces)"))
        code_input = QLineEdit()
        code_input.setObjectName("lipid_code_input")
        new_lipid_layout.addWidget(code_input)

        new_lipid_layout.addWidget(QLabel("Concentration"))
        conc_input = QLineEdit()
        conc_input.setObjectName("lipid_conc_input")
        new_lipid_layout.addWidget(conc_input)

        units_combo = QComboBox()
        units_combo.addItems(["mM", "mg/ml"])
        new_lipid_layout.addWidget(units_combo)

        new_lipid_layout.addWidget(QLabel("MW"))
        mw_input = QLineEdit()
        mw_input.setObjectName("lipid_mw_input")
        mw_input.setEnabled(False)
        new_lipid_layout.addWidget(mw_input)

        def _toggle_mw():
            mw_input.setEnabled(units_combo.currentText() == "mg/ml")
        units_combo.currentTextChanged.connect(_toggle_mw)

        new_lipid_layout.addWidget(QLabel("Color"))
        color_btn = QPushButton("Select Color")
        color_btn.setObjectName("lipid_color_btn")
        color_btn.setStyleSheet("background-color: #CCCCCC;")
        color_btn.clicked.connect(lambda: self._select_lipid_color(color_btn))
        new_lipid_layout.addWidget(color_btn)

        layout.addWidget(new_lipid_group)

        save_btn = QPushButton("Save")
        def _save():
            selected_name = combo.currentData()
            if selected_name:
                cfg = self.control_api.load_lipid_config(selected_name) or {}
                color_hex = cfg.get("color", "#555555")
                self.control_api.set_intake_lipid(plate, row, col, selected_name, color_hex)
                self.intake_wells[(plate, row, col)].set_lipid(selected_name, color_hex)
                dlg.accept()
                return

            name = name_input.text().strip()
            conc = conc_input.text().strip()
            units = units_combo.currentText()
            mw = mw_input.text().strip()
            if not name or not conc:
                QMessageBox.warning(dlg, "Error", "Please fill all fields")
                return
            if units == "mg/ml" and not mw:
                QMessageBox.warning(dlg, "Error", "MW required for mg/ml")
                return

            conc_val = float(conc)
            conc_mM = conc_val if units == "mM" else (conc_val * 1000.0 / float(mw))
            color_hex = color_btn.color.name() if hasattr(color_btn, "color") else "#555555"

            try:
                self.control_api.save_lipid_config(
                    name,
                    {
                        "concentration": conc,
                        "units": units,
                        "mw": mw,
                        "concentration_mM": conc_mM,
                        "color": color_hex,
                        "lipid_code": code_input.text().strip().upper(),
                    },
                )
            except Exception as e:
                QMessageBox.warning(dlg, "Error", str(e))
                return
            self.control_api.set_intake_lipid(plate, row, col, name, color_hex)
            self.intake_wells[(plate, row, col)].set_lipid(name, color_hex)
            dlg.accept()

        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)

        delete_btn = QPushButton("Delete Selected Config")
        def _delete_selected():
            selected_name = combo.currentData()
            if not selected_name:
                QMessageBox.warning(dlg, "Error", "Select a lipid config to delete.")
                return
            self.control_api.delete_lipid_config(selected_name)
            combo.clear()
            combo.addItem("Add New Lipid...", userData=None)
            for name in self.control_api.get_lipid_configs():
                cfg = self.control_api.load_lipid_config(name) or {}
                conc_mM = cfg.get("concentration_mM", "")
                label = f"{name} ({conc_mM} mM)" if conc_mM != "" else name
                combo.addItem(label, userData=name)
        delete_btn.clicked.connect(_delete_selected)
        layout.addWidget(delete_btn)

        # Store references for toggling
        dlg.combo = combo
        dlg.name_input = name_input
        dlg.conc_input = conc_input
        dlg.mw_input = mw_input
        dlg.color_btn = color_btn
        dlg.new_lipid_group = new_lipid_group

        self._toggle_lipid_inputs(dlg)
        dlg.exec()

    def _toggle_lipid_inputs(self, dlg):
        is_new = dlg.combo.currentData() is None
        dlg.new_lipid_group.setEnabled(is_new)

    def _select_lipid_color(self, btn):
        color = QColorDialog.getColor()
        if color.isValid():
            btn.setStyleSheet(f"background-color: {color.name()};")
            btn.color = color

    def _open_buffer_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Buffer Selection")
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Select Buffer Config:"))
        buffer_combo = QComboBox()
        buffer_combo.addItems(self.control_api.get_buffer_configs())
        layout.addWidget(buffer_combo)

        new_btn = QPushButton("Create New")
        new_btn.clicked.connect(lambda: self._create_buffer_config(dlg, buffer_combo))
        layout.addWidget(new_btn)

        save_btn = QPushButton("Save")
        def _save():
            name = buffer_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Error", "Please select a buffer.")
                return
            self.control_api.set_buffer_selected(name)
            self.buffer_btn.setText(f"Buffer: {name}")
            self.buffer_btn.setStyleSheet("background-color: #555555; color: white;")
            dlg.accept()
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)

        dlg.exec()

    def _create_buffer_config(self, parent_dlg, combo=None):
        cfg_dlg = QDialog(parent_dlg)
        cfg_dlg.setWindowTitle("Create Buffer Config")
        layout = QVBoxLayout(cfg_dlg)
        layout.addWidget(QLabel("Buffer Name:"))
        name_input = QLineEdit()
        layout.addWidget(name_input)
        layout.addWidget(QLabel("Concentration:"))
        conc_input = QLineEdit()
        layout.addWidget(conc_input)

        save_btn = QPushButton("Save")
        def _save():
            name = name_input.text().strip()
            conc = conc_input.text().strip()
            if name and conc:
                self.control_api.save_buffer_config(name, {"concentration": conc})
                if combo is not None:
                    combo.clear()
                    combo.addItems(self.control_api.get_buffer_configs())
                    combo.setCurrentText(name)
                cfg_dlg.accept()
            else:
                QMessageBox.warning(cfg_dlg, "Error", "Please fill all fields")
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)
        cfg_dlg.exec()

    def _open_connection_popup(self, device_name):
        if device_name == "Microfluidics":
            self._open_microfluidics_popup()
        elif device_name == "Dobot":
            self._open_dobot_popup()
        elif device_name == "Microcontroller":
            self._open_microcontroller_popup()

    def _open_microfluidics_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Microfluidics Connection")
        dlg.setMinimumWidth(760)
        layout = QVBoxLayout(dlg)
        columns = QHBoxLayout()
        main_col = QVBoxLayout()
        extra_col = QVBoxLayout()
        columns.addLayout(main_col, 1)
        columns.addLayout(extra_col, 1)

        main_col.addWidget(QLabel("Main pressure controller:"))
        config_combo = QComboBox()
        microfluidics_configs = self.control_api.get_microfluidics_configs()
        main_configs = [name for name in microfluidics_configs if name != "Mk4_Extra"] or microfluidics_configs
        config_combo.addItems(main_configs)
        main_col.addWidget(config_combo)

        new_config_btn = QPushButton("Add Device")
        new_config_btn.clicked.connect(lambda: self._create_microfluidics_config(dlg, config_combo))
        main_col.addWidget(new_config_btn)

        main_col.addWidget(QLabel("Calibration:"))
        calib_combo = QComboBox()
        calib_combo.addItems(["Load", "Default", "New"])
        main_col.addWidget(calib_combo)

        main_col.addWidget(QLabel("If 'New' is selected, block gas lines then press Done."))

        extra_col.addWidget(QLabel("Extra pressure controller:"))
        extra_config_combo = QComboBox()
        extra_config_combo.addItem("", userData=None)
        extra_config_combo.addItems(microfluidics_configs)
        extra_config_combo.setCurrentIndex(0)
        extra_col.addWidget(extra_config_combo)

        extra_config_btn = QPushButton("Add Device")
        extra_config_btn.clicked.connect(lambda: self._create_microfluidics_config(dlg, extra_config_combo))
        extra_col.addWidget(extra_config_btn)

        extra_col.addWidget(QLabel("Calibration:"))
        extra_calib_combo = QComboBox()
        extra_calib_combo.addItems(["Load", "Default", "New"])
        extra_col.addWidget(extra_calib_combo)

        extra_col.addStretch(1)

        layout.addLayout(columns)

        done_btn = QPushButton("Done")
        def _connect():
            # Get sensor configs from global config
            cfg = self.control_api.get_config()
            line3_rna_mode = bool(cfg.get("line3_RNA_constant", cfg.get("line3_constant_mode_enabled", False)))
            extra_config_name = str(extra_config_combo.currentText() or "").strip()
            connect_extra = bool(extra_config_name)
            sensor_config = []
            for i in range(1, 5):
                sensor_key = f"sensor{i}"
                if i == 4 and line3_rna_mode and not connect_extra:
                    sensor_key = "sensor_rna"
                sensor_str = cfg.get(sensor_key, None)
                if sensor_str:
                    try:
                        import ast
                        if isinstance(sensor_str, str):
                            sensor_list = ast.literal_eval(sensor_str)
                        elif isinstance(sensor_str, (list, tuple)):
                            sensor_list = list(sensor_str)
                        else:
                            sensor_list = None
                        sensor_config.append(sensor_list)
                    except:
                        print(f"Warning: Could not parse {sensor_key}={sensor_str}")
                        sensor_config.append(None)
                else:
                    sensor_config.append(None)
            
            print(f"[GUI] Connecting with sensor_config: {sensor_config}")
            if connect_extra:
                print(
                    "[GUI] Extra pressure controller selected: "
                    f"config={extra_config_name}, port=COM6, calibration={extra_calib_combo.currentText()}",
                    flush=True,
                )
            else:
                print("[GUI] No extra pressure controller selected.", flush=True)
            ok, err = self.control_api.connect_microfluidics(
                config_combo.currentText(),
                calib_combo.currentText(),
                sensor_config,
                connect_extra_pressure=connect_extra,
                extra_pressure_com_port="COM6",
                extra_config_name=extra_config_name,
                extra_calibration=extra_calib_combo.currentText(),
            )
            if ok:
                self.conn_buttons["Microfluidics"].setStyleSheet("background-color: #7CFC90;")
                self._conn_state["Microfluidics"] = True
                self._update_start_enabled()
                msg = "Microfluidics connected successfully."
                if err:
                    msg += f"\n{err}"
                QMessageBox.information(dlg, "Connected", msg)
                dlg.accept()
            else:
                self._conn_state["Microfluidics"] = False
                self._update_start_enabled()
                QMessageBox.warning(dlg, "Cannot connect", err or "Cannot connect to microfluidics device.")
        done_btn.clicked.connect(_connect)
        layout.addWidget(done_btn)

        dlg.exec()

    def _create_microfluidics_config(self, parent_dlg, combo=None):
        cfg_dlg = QDialog(parent_dlg)
        cfg_dlg.setWindowTitle("Create Microfluidics Config")
        layout = QVBoxLayout(cfg_dlg)
        layout.addWidget(QLabel("Config Name:"))
        name_input = QLineEdit()
        layout.addWidget(name_input)
        layout.addWidget(QLabel("Device ID:"))
        id_input = QLineEdit()
        layout.addWidget(id_input)

        save_btn = QPushButton("Save")
        def _save():
            name = name_input.text().strip()
            device_id = id_input.text().strip()
            if name and device_id:
                self.control_api.save_microfluidics_config(name, {"id": device_id})
                if combo is not None:
                    combo.clear()
                    combo.addItems(self.control_api.get_microfluidics_configs())
                    combo.setCurrentText(name)
                cfg_dlg.accept()
            else:
                QMessageBox.warning(cfg_dlg, "Error", "Please fill all fields")
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)
        cfg_dlg.exec()

    def _open_dobot_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Dobot Connection")
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Select or create config:"))
        config_combo = QComboBox()
        config_combo.addItems(self.control_api.get_dobot_configs())
        layout.addWidget(config_combo)

        new_config_btn = QPushButton("Create New Config")
        new_config_btn.clicked.connect(lambda: self._create_dobot_config(dlg, config_combo))
        layout.addWidget(new_config_btn)

        layout.addWidget(QLabel("Plate calibration file:"))
        calib_path = QLineEdit()
        browse_btn = QPushButton("Browse")
        def _browse():
            path, _ = QFileDialog.getOpenFileName(self, "Select Calibration File", "", "JSON Files (*.json)")
            if path:
                calib_path.setText(path)
        browse_btn.clicked.connect(_browse)
        layout.addWidget(calib_path)
        layout.addWidget(browse_btn)

        connect_btn = QPushButton("Connect")
        def _connect():
            ok, err = self.control_api.connect_dobot(config_combo.currentText(), calib_path.text())
            if ok:
                self.conn_buttons["Dobot"].setStyleSheet("background-color: #7CFC90;")
                self._conn_state["Dobot"] = True
                self._update_start_enabled()
                QMessageBox.information(dlg, "Connected", "Dobot connected successfully.")
                dlg.accept()
            else:
                self._conn_state["Dobot"] = False
                self._update_start_enabled()
                QMessageBox.warning(dlg, "Connection Failed", err or "Cannot connect to Dobot.")
        connect_btn.clicked.connect(_connect)
        layout.addWidget(connect_btn)

        dlg.exec()

    def _create_dobot_config(self, parent_dlg, combo=None):
        cfg_dlg = QDialog(parent_dlg)
        cfg_dlg.setWindowTitle("Create Dobot Config")
        layout = QVBoxLayout(cfg_dlg)
        layout.addWidget(QLabel("Config Name:"))
        name_input = QLineEdit()
        layout.addWidget(name_input)
        layout.addWidget(QLabel("IP Address:"))
        ip_input = QLineEdit()
        layout.addWidget(ip_input)
        layout.addWidget(QLabel("Port:"))
        port_input = QLineEdit()

        save_btn = QPushButton("Save")
        def _save():
            name = name_input.text().strip()
            ip = ip_input.text().strip()
            port = port_input.text().strip()
            if name and ip and port:
                self.control_api.save_dobot_config(name, {"ip": ip, "port": port})
                if combo is not None:
                    combo.clear()
                    combo.addItems(self.control_api.get_dobot_configs())
                    combo.setCurrentText(name)
                cfg_dlg.accept()
            else:
                QMessageBox.warning(cfg_dlg, "Error", "Please fill all fields")
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)
        cfg_dlg.exec()

    def _open_microcontroller_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Microcontroller Connection")
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Main microcontroller COM port:"))
        main_port_combo = QComboBox()
        ports = list(serial.tools.list_ports.comports())
        port_names = [p.device for p in ports]
        main_port_combo.addItems(port_names)
        if "COM4" in port_names:
            main_port_combo.setCurrentText("COM4")
        elif port_names:
            main_port_combo.setCurrentIndex(0)
        layout.addWidget(main_port_combo)

        layout.addWidget(QLabel("Secondary microcontroller COM port (optional):"))
        secondary_port_combo = QComboBox()
        secondary_port_combo.addItem("None")
        secondary_port_combo.addItems(port_names)
        if "COM5" in port_names:
            secondary_port_combo.setCurrentText("COM5")
        else:
            secondary_port_combo.setCurrentText("None")
        layout.addWidget(secondary_port_combo)

        connect_btn = QPushButton("Connect")
        def _connect():
            main_port = main_port_combo.currentText()
            secondary_port = secondary_port_combo.currentText()
            ok, err = self.control_api.connect_microcontroller(main_port, secondary_port)
            if ok:
                self.conn_buttons["Microcontroller"].setStyleSheet("background-color: #7CFC90;")
                self._conn_state["Microcontroller"] = True
                self._update_start_enabled()
                QMessageBox.information(dlg, "Connected", err or "Microcontroller connected successfully.")
                dlg.accept()
            else:
                self._conn_state["Microcontroller"] = False
                self._update_start_enabled()
                QMessageBox.warning(dlg, "Connection Failed", err or "Cannot connect to microcontroller.")

        connect_btn.clicked.connect(_connect)
        layout.addWidget(connect_btn)

        dlg.exec()

    def _open_experiment_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Experiment Designer")
        dlg.setMinimumSize(1200, 585)
        root_layout = QHBoxLayout(dlg)
        left_widget = QWidget(dlg)
        layout = QVBoxLayout(left_widget)
        root_layout.addWidget(left_widget, 1)
        right_comp_panel = self._build_composition_panel()
        right_comp_panel.setMinimumWidth(420)
        root_layout.addWidget(right_comp_panel, 1)
        root_layout.setStretch(0, 1)
        root_layout.setStretch(1, 1)

        # Preset controls
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        preset_combo = QComboBox()
        preset_combo.setEditable(False)
        preset_combo.addItems(self.control_api.list_experiment_presets())
        preset_row.addWidget(preset_combo)
        preset_row.addWidget(QLabel("Name:"))
        preset_name = QLineEdit()
        preset_name.setPlaceholderText("preset name")
        preset_row.addWidget(preset_name)
        save_preset_btn = QPushButton("Save Preset")
        load_preset_btn = QPushButton("Load Preset")
        delete_preset_btn = QPushButton("Delete Preset")
        preset_row.addWidget(save_preset_btn)
        preset_row.addWidget(load_preset_btn)
        preset_row.addWidget(delete_preset_btn)
        layout.addLayout(preset_row)

        layout.addWidget(QLabel("Experiment name"))
        name_input = QLineEdit()
        layout.addWidget(name_input)

        layout.addWidget(QLabel("Parameters"))
        params_layout = QGridLayout()
        lipid_flow_spin = QDoubleSpinBox()
        lipid_flow_spin.setMaximum(1000)
        lipid_flow_spin.setDecimals(3)
        lipid_flow_spin.setSuffix(" uL/min")
        params_layout.addWidget(QLabel("Lipid Flow Rate:"), 0, 0)
        params_layout.addWidget(lipid_flow_spin, 0, 1)
        buffer_flow_spin = QDoubleSpinBox()
        buffer_flow_spin.setMaximum(1000)
        buffer_flow_spin.setDecimals(3)
        buffer_flow_spin.setSuffix(" uL/min")
        params_layout.addWidget(QLabel("Buffer Flow Rate:"), 1, 0)
        params_layout.addWidget(buffer_flow_spin, 1, 1)
        vol_spin = QDoubleSpinBox()
        vol_spin.setMaximum(1000)
        vol_spin.setSuffix(" µL")
        params_layout.addWidget(QLabel("Volume:"), 2, 0)
        params_layout.addWidget(vol_spin, 2, 1)
        rep_spin = QSpinBox()
        rep_spin.setMinimum(1)
        rep_spin.setValue(1)
        params_layout.addWidget(QLabel("Repeats:"), 3, 0)
        params_layout.addWidget(rep_spin, 3, 1)
        layout.addLayout(params_layout)

        cfg_now = self.control_api.get_config() or {}
        line3_feature_enabled = bool(cfg_now.get("line3_RNA_constant", cfg_now.get("line3_constant_mode_enabled", False)))
        line3_uses_main_pump = bool(line3_feature_enabled and not self.control_api.is_extra_pressure_connected())
        line3_const_rate = QDoubleSpinBox()
        line3_const_rate.setRange(0.0, 1000.0)
        line3_const_rate.setDecimals(2)
        line3_const_rate.setSuffix(" uL/min")
        line3_const_rate.setValue(0.0)
        if line3_feature_enabled:
            line3_const_row = QHBoxLayout()
            line3_const_row.addWidget(QLabel("RNA flow rate (Line 3):"))
            line3_const_row.addWidget(line3_const_rate)
            layout.addLayout(line3_const_row)

        layout.addWidget(QLabel("Lipid Stocks"))
        lipid_layout = QHBoxLayout()
        lipid_combos = []
        lipid_names = self.control_api.get_lipid_configs()
        for i in range(3):
            cb = QComboBox()
            cb.addItem("", userData=None)
            for name in lipid_names:
                cfg = self.control_api.load_lipid_config(name) or {}
                conc_mM = cfg.get("concentration_mM", "")
                label = f"{name} ({conc_mM} mM)" if conc_mM != "" else name
                cb.addItem(label, userData=name)
            lipid_layout.addWidget(cb)
            lipid_combos.append(cb)
        layout.addLayout(lipid_layout)

        # Connect combo changes to validate duplicates
        def _validate_duplicates():
            selected = [cb.currentData() for cb in lipid_combos]
            selected = [s for s in selected if s]  # filter out None
            
            # Highlight duplicates in red
            for cb in lipid_combos:
                name = cb.currentData()
                if name and selected.count(name) > 1:
                    cb.setStyleSheet("background-color: #FF6B6B;")
                else:
                    cb.setStyleSheet("")
        
        for cb in lipid_combos:
            cb.currentTextChanged.connect(_validate_duplicates)

        lipid_change_guard = {
            "suspend": False,
            "prev_indices": [cb.currentIndex() for cb in lipid_combos],
            "prev_count": sum(1 for cb in lipid_combos if cb.currentData()),
        }

        def _on_lipid_selection_changed():
            if lipid_change_guard["suspend"]:
                return
            new_indices = [cb.currentIndex() for cb in lipid_combos]
            new_count = sum(1 for cb in lipid_combos if cb.currentData())
            old_count = int(lipid_change_guard.get("prev_count", 0))
            if new_count != old_count and len(getattr(self, "_composition_table_data", [])) > 0:
                resp = QMessageBox.warning(
                    dlg,
                    "Component Lipids Changed",
                    "Component lipid count changed.\nAll compositions will be erased.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    lipid_change_guard["suspend"] = True
                    try:
                        for i, cb in enumerate(lipid_combos):
                            cb.setCurrentIndex(int(lipid_change_guard["prev_indices"][i]))
                    finally:
                        lipid_change_guard["suspend"] = False
                    return
                self._composition_table_data = []
                self._refresh_composition_preview_panel()
            lipid_change_guard["prev_indices"] = new_indices
            lipid_change_guard["prev_count"] = new_count

        for cb in lipid_combos:
            cb.currentIndexChanged.connect(_on_lipid_selection_changed)

        def _sync_line3_constant_mode():
            enabled = bool(line3_uses_main_pump)
            if len(lipid_combos) >= 3:
                lipid_combos[2].setEnabled(not enabled)
                if enabled:
                    lipid_combos[2].setCurrentIndex(0)
            _validate_duplicates()

        if line3_feature_enabled:
            line3_const_rate.valueChanged.connect(lambda _: _sync_line3_constant_mode())
        _sync_line3_constant_mode()

        exp_state = {"lipid_stocks": [], "set": False}

        set_btn = QPushButton("Set")
        def _set_lipids():
            lipid_stocks = []
            selected_names = []
            for cb in lipid_combos:
                name = cb.currentData()
                if not name:
                    continue
                
                # Check for duplicate
                if name in selected_names:
                    QMessageBox.warning(dlg, "Error", f"Lipid '{name}' cannot be used on multiple lines")
                    return
                
                selected_names.append(name)
                cfg = self.control_api.load_lipid_config(name) or {}
                lipid_stocks.append({
                    "name": name,
                    "concentration": cfg.get("concentration_mM", cfg.get("concentration", "")),
                    "mw": cfg.get("mw", ""),
                    "color": cfg.get("color", "#555555")
                })
            if not lipid_stocks:
                QMessageBox.warning(dlg, "Error", "Please select at least one lipid.")
                return
            exp_state["lipid_stocks"] = lipid_stocks
            exp_state["set"] = True
            for cb in lipid_combos:
                cb.setEnabled(False)
            set_btn.setEnabled(False)
            space_combo.setEnabled(True)
            self._update_screen_space(space_combo.currentText())
        set_btn.clicked.connect(_set_lipids)
        layout.addWidget(set_btn)

        space_combo = QComboBox()
        space_combo.addItems(["Manual", "Load", "Scan"])
        space_combo.setEnabled(False)

        self._manual_compositions = []
        self._scan_extra_compositions = []
        self._load_extra_compositions = []
        self._composition_table_data = []
        self._screen_space_container = QVBoxLayout()
        screen_row = QHBoxLayout()
        left_screen = QVBoxLayout()
        left_screen.addWidget(QLabel("Screen Space"))
        left_screen.addWidget(space_combo)
        left_screen.addLayout(self._screen_space_container)
        screen_row.addLayout(left_screen, 1)
        layout.addLayout(screen_row)
        self._lipid_combos = lipid_combos
        self._space_combo = space_combo
        space_combo.currentTextChanged.connect(self._update_screen_space)

        def _refresh_presets():
            preset_combo.clear()
            preset_combo.addItems(self.control_api.list_experiment_presets())

        def _apply_preset(exp_data: dict):
            # Basic params
            name_input.setText(exp_data.get("name", ""))
            tfr_val = float(exp_data.get("tfr", 0) or 0)
            frr_val = float(exp_data.get("frr", 0) or 0)
            lipid_fr = (tfr_val / frr_val) if frr_val else 0.0
            buffer_fr = max(tfr_val - lipid_fr, 0.0)
            lipid_flow_spin.setValue(float(lipid_fr))
            buffer_flow_spin.setValue(float(buffer_fr))
            vol_spin.setValue(float(exp_data.get("volume", 0) or 0))
            rep_spin.setValue(int(exp_data.get("repeats", 1) or 1))
            line3_rate = float(exp_data.get("line3_constant_flow_rate", 0.0) or 0.0) if line3_feature_enabled else 0.0
            line3_const_rate.setValue(line3_rate)
            _sync_line3_constant_mode()
            # Reset lipid selection
            for cb in lipid_combos:
                cb.setEnabled(True)
                cb.setCurrentIndex(0)
            _sync_line3_constant_mode()
            set_btn.setEnabled(True)
            exp_state["set"] = False
            exp_state["lipid_stocks"] = []

            # Apply lipid stocks
            lipid_stocks = exp_data.get("lipid_stocks") or []
            lipid_change_guard["suspend"] = True
            try:
                for i, lipid in enumerate(lipid_stocks):
                    if i >= len(lipid_combos):
                        break
                    name = lipid.get("name")
                    if not name:
                        continue
                    cb = lipid_combos[i]
                    for j in range(cb.count()):
                        if cb.itemData(j) == name:
                            cb.setCurrentIndex(j)
                            break
            finally:
                lipid_change_guard["suspend"] = False
            lipid_change_guard["prev_indices"] = [cb.currentIndex() for cb in lipid_combos]
            lipid_change_guard["prev_count"] = sum(1 for cb in lipid_combos if cb.currentData())

            _set_lipids()

            # Screen space
            mode = exp_data.get("screen_space_mode", "Manual")
            space_combo.setEnabled(True)
            space_combo.setCurrentText(mode)
            self._update_screen_space(mode)

            params = exp_data.get("screen_space_params", {}) or {}
            if mode == "Load":
                if hasattr(self, "_load_path") and params.get("path"):
                    self._load_path.setText(params.get("path"))
            elif mode == "Scan":
                min_vals = params.get("min_vals", [])
                max_vals = params.get("max_vals", [])
                interval = params.get("interval", None)
                for i in range(3):
                    if hasattr(self, "_scan_min") and i < len(self._scan_min) and i < len(min_vals):
                        self._scan_min[i].setValue(float(min_vals[i]))
                    if hasattr(self, "_scan_max") and i < len(self._scan_max) and i < len(max_vals):
                        self._scan_max[i].setValue(float(max_vals[i]))
                if hasattr(self, "_scan_interval") and interval is not None:
                    self._scan_interval.setValue(float(interval))
            table_comps = params.get("compositions", exp_data.get("compositions", [])) or []
            self._composition_table_data = [self._normalize_comp(c) for c in table_comps]
            if hasattr(self, "_manual_count_label"):
                self._manual_count_label.setText(f"Compositions: {len(self._composition_table_data)}")
            self._refresh_composition_preview_panel()
        def _save_preset():
            name = preset_name.text().strip()
            if not name:
                QMessageBox.warning(dlg, "Name Required", "Enter a preset name to save.")
                return
            if name in self.control_api.list_experiment_presets():
                resp = QMessageBox.question(
                    dlg,
                    "Overwrite Preset",
                    f"Preset '{name}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    return

            # Recompute lipid stocks from current selection
            lipid_stocks = []
            selected_names = []
            for cb in lipid_combos:
                name = cb.currentData()
                if not name:
                    continue
                if name in selected_names:
                    QMessageBox.warning(dlg, "Error", f"Lipid '{name}' cannot be used on multiple lines")
                    return
                selected_names.append(name)
                cfg = self.control_api.load_lipid_config(name) or {}
                lipid_stocks.append({
                    "name": name,
                    "concentration": cfg.get("concentration_mM", cfg.get("concentration", "")),
                    "mw": cfg.get("mw", ""),
                    "color": cfg.get("color", "#555555")
                })
            if not lipid_stocks:
                QMessageBox.warning(dlg, "Error", "Please select at least one lipid.")
                return

            mode = space_combo.currentText()
            if line3_feature_enabled and line3_const_rate.value() <= 0.0:
                QMessageBox.warning(dlg, "Error", "Set RNA flow rate (> 0) for line 3 constant mode.")
                return
            lipid_fr_val = float(lipid_flow_spin.value())
            buffer_fr_val = float(buffer_flow_spin.value())
            if lipid_fr_val <= 0:
                QMessageBox.warning(dlg, "Error", "Lipid Flow Rate must be > 0.")
                return
            tfr_val = float(lipid_fr_val + buffer_fr_val)
            frr_val = float(tfr_val / lipid_fr_val)

            exp_data = {
                "name": name_input.text().strip() or "Untitled",
                "buffer": {"name": "Buffer", "concentration": 0},
                "lipid_stocks": lipid_stocks,
                "tfr": tfr_val,
                "frr": frr_val,
                "volume": vol_spin.value(),
                "repeats": rep_spin.value(),
                "screen_space_mode": mode,
                "screen_space_params": self._get_screen_space_params(mode),
                "line3_constant_flow_enabled": bool(line3_feature_enabled and line3_const_rate.value() > 0.0),
                "line3_constant_flow_rate": float(line3_const_rate.value() if line3_feature_enabled and line3_const_rate.value() > 0.0 else 0.0),
            }
            ok, err = self.control_api.save_experiment_preset(name, exp_data=exp_data)
            if not ok:
                QMessageBox.warning(dlg, "Save Failed", err or "Failed to save preset.")
                return
            _refresh_presets()
            preset_combo.setCurrentText(name)
            QMessageBox.information(dlg, "Saved", f"Preset '{name}' saved.")

        def _load_preset():
            name = preset_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Select Preset", "Select a preset to load.")
                return
            exp_data = self.control_api.load_experiment_preset(name)
            if not exp_data:
                QMessageBox.warning(dlg, "Not Found", f"Preset '{name}' not found.")
                return
            _apply_preset(exp_data)

        def _delete_preset():
            name = preset_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Select Preset", "Select a preset to delete.")
                return
            resp = QMessageBox.question(
                dlg,
                "Delete Preset",
                f"Delete preset '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
            # reuse config manager delete via control API
            self.control_api.delete_experiment_preset(name)
            _refresh_presets()
            QMessageBox.information(dlg, "Deleted", f"Preset '{name}' deleted.")

        save_preset_btn.clicked.connect(_save_preset)
        load_preset_btn.clicked.connect(_load_preset)
        delete_preset_btn.clicked.connect(_delete_preset)

        save_btn = QPushButton("Save")
        def _save():
            if not exp_state["set"]:
                QMessageBox.warning(dlg, "Error", "Please set lipid stocks before continuing.")
                return

            mode = space_combo.currentText()
            if any(abs(sum(comp) - 100) > 1 for comp in getattr(self, "_composition_table_data", [])):
                QMessageBox.warning(
                    dlg,
                    "Composition Warning",
                    "One or more compositions do not sum to 100 (+/-1). The experiment will still be saved.",
                )

            if line3_feature_enabled and line3_const_rate.value() <= 0.0:
                QMessageBox.warning(dlg, "Error", "Set RNA flow rate (> 0) for line 3 constant mode.")
                return
            lipid_fr_val = float(lipid_flow_spin.value())
            buffer_fr_val = float(buffer_flow_spin.value())
            if lipid_fr_val <= 0:
                QMessageBox.warning(dlg, "Error", "Lipid Flow Rate must be > 0.")
                return
            tfr_val = float(lipid_fr_val + buffer_fr_val)
            frr_val = float(tfr_val / lipid_fr_val)

            exp_data = {
                "name": name_input.text().strip() or "Untitled",
                "buffer": {"name": "Buffer", "concentration": 0},
                "lipid_stocks": exp_state["lipid_stocks"],
                "tfr": tfr_val,
                "frr": frr_val,
                "volume": vol_spin.value(),
                "repeats": rep_spin.value(),
                "screen_space_mode": mode,
                "screen_space_params": self._get_screen_space_params(mode),
                "line3_constant_flow_enabled": bool(line3_feature_enabled and line3_const_rate.value() > 0.0),
                "line3_constant_flow_rate": float(line3_const_rate.value() if line3_feature_enabled and line3_const_rate.value() > 0.0 else 0.0),
            }
            self.control_api.add_experiment_to_queue(exp_data)
            self._refresh_queue_table()
            dlg.accept()
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)

        dlg.exec()

    def _update_selected_lipids_label(self, lipid_combos):
        names = []
        for cb in lipid_combos:
            name = cb.currentData()
            if name:
                names.append(name)
        self._exp_lipid_name_label.setText(f"Selected lipids: {', '.join(names) if names else '-'}")

    def _get_selected_lipid_names(self):
        names = []
        for cb in getattr(self, "_lipid_combos", []):
            name = cb.currentData()
            if name:
                names.append(name)
        return names

    def _update_screen_space(self, mode):
        self._clear_layout(self._screen_space_container)

        selected = self._get_selected_lipid_names()
        n_sel = len(selected)

        if mode == "Manual":
            grid = QGridLayout()
            for i in range(3):
                lbl = QLabel(selected[i] if i < len(selected) else "None")
                grid.addWidget(lbl, 0, i)
            self._manual_inputs = [QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()]
            for idx, spin in enumerate(self._manual_inputs):
                spin.setRange(0, 100)
                spin.setEnabled(idx < n_sel)
                grid.addWidget(spin, 1, idx)
            self._screen_space_container.addLayout(grid)

            row = QHBoxLayout()
            add_btn = QPushButton("Add")
            add_btn.setEnabled(n_sel > 0)
            count_label = QLabel("Compositions: 0")
            self._manual_count_label = count_label

            def _add():
                vals = [s.value() for s in self._manual_inputs]
                if abs(sum(vals) - 100) > 1:
                    QMessageBox.warning(
                        self,
                        "Composition Warning",
                        "Manual composition does not sum to 100 (+/-1). It will still be added.",
                    )
                self._composition_table_data = list(getattr(self, "_composition_table_data", []))
                self._composition_table_data.append(self._normalize_comp(vals))
                count_label.setText(f"Compositions: {len(self._composition_table_data)}")
                self._refresh_composition_preview_panel()

            add_btn.clicked.connect(_add)
            row.addWidget(add_btn)
            row.addWidget(count_label)
            self._screen_space_container.addLayout(row)

        elif mode == "Load":
            self._load_path = QLineEdit()
            btn = QPushButton("Browse File")
            add_btn = QPushButton("Add")
            def _browse():
                path, _ = QFileDialog.getOpenFileName(self, "Load Compositions", "", "CSV Files (*.csv)")
                if path:
                    self._load_path.setText(path)
            self._load_path.textChanged.connect(lambda _: self._refresh_composition_preview_panel())
            btn.clicked.connect(_browse)
            self._screen_space_container.addWidget(self._load_path)
            row = QHBoxLayout()
            row.addWidget(btn)
            row.addWidget(add_btn)
            self._screen_space_container.addLayout(row)

            def _add_loaded():
                path = self._load_path.text().strip()
                if not path:
                    QMessageBox.warning(self, "Error", "Select a CSV file first.")
                    return
                comps = self._parse_compositions_csv(path)
                if not comps:
                    QMessageBox.warning(self, "Error", "No valid compositions found in file.")
                    return
                self._composition_table_data = list(getattr(self, "_composition_table_data", []))
                self._composition_table_data.extend([self._normalize_comp(c) for c in comps])
                self._refresh_composition_preview_panel()

            add_btn.clicked.connect(_add_loaded)

        elif mode == "Scan":
            self._scan_min = [QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()]
            self._scan_max = [QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()]
            for i in range(3):
                row = QHBoxLayout()
                row.setSpacing(4)
                row.setContentsMargins(0, 0, 0, 0)
                if i == 0:
                    row.addWidget(QLabel("Base composition"))
                    self._scan_min[i].setEnabled(False)
                    self._scan_max[i].setEnabled(False)
                else:
                    row.addWidget(QLabel(selected[i] if i < len(selected) else f"Lipid {i+1}"))
                self._scan_min[i].setMaximum(100)
                self._scan_max[i].setMaximum(100)
                self._scan_min[i].setValue(0)
                self._scan_max[i].setValue(100)
                self._scan_min[i].setEnabled(i < n_sel and i != 0)
                self._scan_max[i].setEnabled(i < n_sel and i != 0)
                self._scan_min[i].valueChanged.connect(lambda _: self._refresh_composition_preview_panel())
                self._scan_max[i].valueChanged.connect(lambda _: self._refresh_composition_preview_panel())
                row.addWidget(QLabel("Min"))
                row.addWidget(self._scan_min[i])
                row.addWidget(QLabel("Max"))
                row.addWidget(self._scan_max[i])
                self._screen_space_container.addLayout(row)

            self._scan_interval = QDoubleSpinBox()
            self._scan_interval.setValue(10)
            self._scan_interval.valueChanged.connect(lambda _: self._refresh_composition_preview_panel())
            self._screen_space_container.addWidget(QLabel("Interval"))
            self._screen_space_container.addWidget(self._scan_interval)
            add_scan_btn = QPushButton("Add")
            self._screen_space_container.addWidget(add_scan_btn)

            def _add_scan():
                comps = self._generate_scan_compositions_preview()
                if not comps:
                    QMessageBox.warning(self, "Error", "No scan compositions generated.")
                    return
                self._composition_table_data = list(getattr(self, "_composition_table_data", []))
                self._composition_table_data.extend([self._normalize_comp(c) for c in comps])
                self._refresh_composition_preview_panel()

            add_scan_btn.clicked.connect(_add_scan)

        self._refresh_composition_preview_panel()

    def _normalize_comp(self, comp):
        vals = list(comp or [])
        while len(vals) < 3:
            vals.append(0.0)
        return [float(vals[0]), float(vals[1]), float(vals[2])]

    def _parse_compositions_csv(self, path: str):
        import csv
        comps = []
        if not path:
            return comps
        try:
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    raw = []
                    for cell in row[:3]:
                        try:
                            raw.append(float(str(cell).strip()))
                        except Exception:
                            raw.append(0.0)
                    if any(abs(v) > 0 for v in raw):
                        comps.append(self._normalize_comp(raw))
        except Exception:
            return []
        return comps

    def _generate_scan_compositions_preview(self):
        import numpy as np
        try:
            selected = self._get_selected_lipid_names()
            n_sel = max(1, len(selected))
            min_vals = [s.value() for s in getattr(self, "_scan_min", [])]
            max_vals = [s.value() for s in getattr(self, "_scan_max", [])]
            interval = float(getattr(self, "_scan_interval", QDoubleSpinBox()).value() or 10.0)
            if interval <= 0:
                interval = 10.0
            min_vals = (min_vals + [0, 0, 0])[:n_sel]
            max_vals = (max_vals + [100, 100, 100])[:n_sel]
            out = []
            if n_sel == 1:
                out.append([100.0, 0.0, 0.0])
            elif n_sel == 2:
                for b in np.arange(min_vals[1], max_vals[1] + interval, interval):
                    a = 100.0 - b
                    if min_vals[0] <= a <= max_vals[0]:
                        out.append([a, b, 0.0])
            else:
                for b in np.arange(min_vals[1], max_vals[1] + interval, interval):
                    for c in np.arange(min_vals[2], max_vals[2] + interval, interval):
                        a = 100.0 - b - c
                        if a >= 0 and min_vals[0] <= a <= max_vals[0]:
                            out.append([a, b, c])
            return [self._normalize_comp(c) for c in out]
        except Exception:
            return []

    def _current_preview_compositions(self):
        entries = []
        for c in getattr(self, "_composition_table_data", []):
            entries.append({"comp": self._normalize_comp(c)})
        return entries

    def _refresh_composition_preview_panel(self):
        table = getattr(self, "_composition_table", None)
        if table is None:
            return
        entries = self._current_preview_compositions()
        self._composition_entries = entries
        table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            comp = e["comp"]
            table.setItem(i, 0, QTableItem(str(i + 1)))
            table.setItem(i, 1, QTableItem(f"{comp[0]:.2f}"))
            table.setItem(i, 2, QTableItem(f"{comp[1]:.2f}"))
            table.setItem(i, 3, QTableItem(f"{comp[2]:.2f}"))
        if hasattr(self, "_comp_count_label"):
            self._comp_count_label.setText(f"Compositions: {len(entries)}")

    def _build_composition_panel(self):
        panel = QGroupBox("Compositions")
        v = QVBoxLayout(panel)
        self._comp_count_label = QLabel("Compositions: 0")
        v.addWidget(self._comp_count_label)

        self._composition_table = QTableWidget(0, 4)
        self._composition_table.setHorizontalHeaderLabels(["#", "L1%", "L2%", "L3%"])
        self._composition_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._composition_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._composition_table.verticalHeader().setVisible(False)
        self._composition_table.setMinimumWidth(360)
        v.addWidget(self._composition_table)

        btn_row = QHBoxLayout()
        up_btn = QPushButton("Up")
        down_btn = QPushButton("Down")
        dup_btn = QPushButton("Duplicate")
        del_btn = QPushButton("Delete")
        reverse_btn = QPushButton("Reverse")
        clear_btn = QPushButton("Clear")
        for b in (up_btn, down_btn, dup_btn, del_btn, reverse_btn, clear_btn):
            btn_row.addWidget(b)
        v.addLayout(btn_row)

        def _selected_row():
            sm = self._composition_table.selectionModel()
            rows = sm.selectedRows() if sm else []
            return rows[0].row() if rows else -1

        def _apply_manual(comps, keep_row=None):
            self._composition_table_data = [self._normalize_comp(c) for c in comps]
            self._refresh_composition_preview_panel()
            if keep_row is not None and 0 <= keep_row < self._composition_table.rowCount():
                self._composition_table.selectRow(keep_row)

        def _up():
            i = _selected_row()
            if i <= 0:
                return
            comps = list(getattr(self, "_composition_table_data", []))
            comps[i - 1], comps[i] = comps[i], comps[i - 1]
            _apply_manual(comps, i - 1)

        def _down():
            i = _selected_row()
            if i < 0:
                return
            comps = list(getattr(self, "_composition_table_data", []))
            if i >= len(comps) - 1:
                return
            comps[i + 1], comps[i] = comps[i], comps[i + 1]
            _apply_manual(comps, i + 1)

        def _dup():
            i = _selected_row()
            if i < 0:
                return
            comps = list(getattr(self, "_composition_table_data", []))
            comps.insert(i + 1, list(comps[i]))
            _apply_manual(comps, i + 1)

        def _del():
            i = _selected_row()
            if i < 0:
                return
            comps = list(getattr(self, "_composition_table_data", []))
            if 0 <= i < len(comps):
                comps.pop(i)
            _apply_manual(comps, max(0, i - 1))

        def _reverse():
            comps = list(getattr(self, "_composition_table_data", []))
            if len(comps) < 2:
                return
            selected = _selected_row()
            new_selected = (len(comps) - 1 - selected) if selected >= 0 else None
            _apply_manual(list(reversed(comps)), new_selected)

        def _clear():
            self._composition_table_data = []
            self._refresh_composition_preview_panel()
        up_btn.clicked.connect(_up)
        down_btn.clicked.connect(_down)
        dup_btn.clicked.connect(_dup)
        del_btn.clicked.connect(_del)
        reverse_btn.clicked.connect(_reverse)
        clear_btn.clicked.connect(_clear)
        return panel

    def _get_screen_space_params(self, mode):
        table_comps = [self._normalize_comp(c) for c in getattr(self, "_composition_table_data", [])]
        if mode == "Manual":
            return {"compositions": table_comps}
        if mode == "Load":
            path = getattr(self, "_load_path", QLineEdit()).text()
            return {
                "path": path,
                "compositions": table_comps,
                "extra_compositions": [],
            }
        if mode == "Scan":
            return {
                "min_vals": [s.value() for s in self._scan_min],
                "max_vals": [s.value() for s in self._scan_max],
                "interval": getattr(self, "_scan_interval", QDoubleSpinBox()).value(),
                "compositions": table_comps,
                "extra_compositions": [],
            }
        return {}

    def _open_experiment_edit_dialog(self, exp_id: str, exp):
        """Edit an existing experiment (only if pending)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit Experiment: {exp.name}")
        dlg.setMinimumSize(1200, 585)
        root_layout = QHBoxLayout(dlg)
        left_widget = QWidget(dlg)
        layout = QVBoxLayout(left_widget)
        root_layout.addWidget(left_widget, 1)
        right_comp_panel = self._build_composition_panel()
        right_comp_panel.setMinimumWidth(420)
        root_layout.addWidget(right_comp_panel, 1)
        root_layout.setStretch(0, 1)
        root_layout.setStretch(1, 1)

        # Preset controls
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        preset_combo = QComboBox()
        preset_combo.setEditable(False)
        preset_combo.addItems(self.control_api.list_experiment_presets())
        preset_row.addWidget(preset_combo)
        preset_row.addWidget(QLabel("Name:"))
        preset_name = QLineEdit()
        preset_name.setPlaceholderText("preset name")
        preset_row.addWidget(preset_name)
        save_preset_btn = QPushButton("Save Preset")
        load_preset_btn = QPushButton("Load Preset")
        delete_preset_btn = QPushButton("Delete Preset")
        preset_row.addWidget(save_preset_btn)
        preset_row.addWidget(load_preset_btn)
        preset_row.addWidget(delete_preset_btn)
        layout.addLayout(preset_row)

        layout.addWidget(QLabel("Experiment name"))
        name_input = QLineEdit()
        name_input.setText(exp.name)
        layout.addWidget(name_input)

        layout.addWidget(QLabel("Parameters"))
        params_layout = QGridLayout()
        lipid_flow_spin = QDoubleSpinBox()
        lipid_flow_spin.setMaximum(1000)
        lipid_flow_spin.setDecimals(3)
        lipid_flow_spin.setSuffix(" uL/min")
        exp_lipid_fr = (float(exp.tfr) / float(exp.frr)) if float(exp.frr) else 0.0
        exp_buffer_fr = max(float(exp.tfr) - exp_lipid_fr, 0.0)
        lipid_flow_spin.setValue(exp_lipid_fr)
        params_layout.addWidget(QLabel("Lipid Flow Rate:"), 0, 0)
        params_layout.addWidget(lipid_flow_spin, 0, 1)
        
        buffer_flow_spin = QDoubleSpinBox()
        buffer_flow_spin.setMaximum(1000)
        buffer_flow_spin.setDecimals(3)
        buffer_flow_spin.setSuffix(" uL/min")
        buffer_flow_spin.setValue(exp_buffer_fr)
        params_layout.addWidget(QLabel("Buffer Flow Rate:"), 1, 0)
        params_layout.addWidget(buffer_flow_spin, 1, 1)
        
        vol_spin = QDoubleSpinBox()
        vol_spin.setMaximum(1000)
        vol_spin.setSuffix(" µL")
        vol_spin.setValue(exp.volume)
        params_layout.addWidget(QLabel("Volume:"), 2, 0)
        params_layout.addWidget(vol_spin, 2, 1)
        
        rep_spin = QSpinBox()
        rep_spin.setMinimum(1)
        rep_spin.setValue(exp.repeats)
        params_layout.addWidget(QLabel("Repeats:"), 3, 0)
        params_layout.addWidget(rep_spin, 3, 1)
        layout.addLayout(params_layout)

        cfg_now = self.control_api.get_config() or {}
        line3_feature_enabled = bool(cfg_now.get("line3_RNA_constant", cfg_now.get("line3_constant_mode_enabled", False)))
        line3_uses_main_pump = bool(line3_feature_enabled and not self.control_api.is_extra_pressure_connected())
        line3_const_rate = QDoubleSpinBox()
        line3_const_rate.setRange(0.0, 1000.0)
        line3_const_rate.setDecimals(2)
        line3_const_rate.setSuffix(" uL/min")
        current_line3_rate = float(getattr(exp, "line3_constant_flow_rate", 0.0) or 0.0) if line3_feature_enabled else 0.0
        line3_const_rate.setValue(current_line3_rate)
        if line3_feature_enabled:
            line3_const_row = QHBoxLayout()
            line3_const_row.addWidget(QLabel("RNA flow rate (Line 3):"))
            line3_const_row.addWidget(line3_const_rate)
            layout.addLayout(line3_const_row)

        layout.addWidget(QLabel("Lipid Stocks"))
        lipid_layout = QHBoxLayout()
        lipid_combos = []
        lipid_names = self.control_api.get_lipid_configs()
        
        # Pre-populate with current lipids
        for i in range(3):
            cb = QComboBox()
            cb.addItem("", userData=None)
            for name in lipid_names:
                cfg = self.control_api.load_lipid_config(name) or {}
                conc_mM = cfg.get("concentration_mM", "")
                label = f"{name} ({conc_mM} mM)" if conc_mM != "" else name
                cb.addItem(label, userData=name)
            
            # Set current value if this lipid slot has a lipid
            if i < len(exp.lipid_stocks):
                current_name = exp.lipid_stocks[i]["name"]
                for j in range(cb.count()):
                    if cb.itemData(j) == current_name:
                        cb.setCurrentIndex(j)
                        break
            
            lipid_layout.addWidget(cb)
            lipid_combos.append(cb)
        layout.addLayout(lipid_layout)

        # Duplicate validation logic
        def _validate_duplicates():
            selected = [cb.currentData() for cb in lipid_combos]
            selected = [s for s in selected if s]
            for cb in lipid_combos:
                name = cb.currentData()
                if name and selected.count(name) > 1:
                    cb.setStyleSheet("background-color: #FF6B6B;")
                else:
                    cb.setStyleSheet("")
        
        for cb in lipid_combos:
            cb.currentTextChanged.connect(_validate_duplicates)

        lipid_change_guard = {
            "suspend": False,
            "prev_indices": [cb.currentIndex() for cb in lipid_combos],
            "prev_count": sum(1 for cb in lipid_combos if cb.currentData()),
        }

        def _on_lipid_selection_changed():
            if lipid_change_guard["suspend"]:
                return
            new_indices = [cb.currentIndex() for cb in lipid_combos]
            new_count = sum(1 for cb in lipid_combos if cb.currentData())
            old_count = int(lipid_change_guard.get("prev_count", 0))
            if new_count != old_count and len(getattr(self, "_composition_table_data", [])) > 0:
                resp = QMessageBox.warning(
                    dlg,
                    "Component Lipids Changed",
                    "Component lipid count changed.\nAll compositions will be erased.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    lipid_change_guard["suspend"] = True
                    try:
                        for i, cb in enumerate(lipid_combos):
                            cb.setCurrentIndex(int(lipid_change_guard["prev_indices"][i]))
                    finally:
                        lipid_change_guard["suspend"] = False
                    return
                self._composition_table_data = []
                self._refresh_composition_preview_panel()
            lipid_change_guard["prev_indices"] = new_indices
            lipid_change_guard["prev_count"] = new_count

        for cb in lipid_combos:
            cb.currentIndexChanged.connect(_on_lipid_selection_changed)

        def _sync_line3_constant_mode():
            enabled = bool(line3_uses_main_pump)
            if len(lipid_combos) >= 3:
                lipid_combos[2].setEnabled(not enabled)
                if enabled:
                    lipid_combos[2].setCurrentIndex(0)
            _validate_duplicates()

        if line3_feature_enabled:
            line3_const_rate.valueChanged.connect(lambda _: _sync_line3_constant_mode())
        _sync_line3_constant_mode()

        exp_state = {"lipid_stocks": exp.lipid_stocks, "set": True}

        # Screen space and composition editing
        space_combo = QComboBox()
        space_combo.addItems(["Manual", "Load", "Scan"])
        space_combo.setCurrentText(exp.screen_space_mode)

        existing_params = dict(getattr(exp, "screen_space_params", {}) or {})
        base_from_params = existing_params.get("compositions", []) or []
        if base_from_params:
            base_comps = [self._normalize_comp(c) for c in base_from_params]
        else:
            # Backward-compatible fallback for legacy queued items missing compositions in params:
            # each base composition is repeated contiguously `repeats` times.
            rep = max(1, int(getattr(exp, "repeats", 1) or 1))
            expanded = [self._normalize_comp(c) for c in (getattr(exp, "compositions", []) or [])]
            base_comps = [expanded[i] for i in range(0, len(expanded), rep)] if expanded else []
        self._manual_compositions = [list(c) for c in base_comps]
        self._scan_extra_compositions = []
        self._load_extra_compositions = []
        self._composition_table_data = [self._normalize_comp(c) for c in base_comps]
        self._screen_space_container = QVBoxLayout()
        screen_row = QHBoxLayout()
        left_screen = QVBoxLayout()
        left_screen.addWidget(QLabel("Screen Space"))
        left_screen.addWidget(space_combo)
        left_screen.addLayout(self._screen_space_container)
        screen_row.addLayout(left_screen, 1)
        layout.addLayout(screen_row)
        self._lipid_combos = lipid_combos
        self._space_combo = space_combo
        space_combo.currentTextChanged.connect(self._update_screen_space)
        self._update_screen_space(exp.screen_space_mode)
        # Preserve existing screen-space params when opening edit dialog
        if exp.screen_space_mode == "Load":
            if hasattr(self, "_load_path") and existing_params.get("path"):
                self._load_path.setText(existing_params.get("path"))
        elif exp.screen_space_mode == "Scan":
            min_vals = existing_params.get("min_vals", [])
            max_vals = existing_params.get("max_vals", [])
            interval = existing_params.get("interval", None)
            for i in range(3):
                if hasattr(self, "_scan_min") and i < len(self._scan_min) and i < len(min_vals):
                    self._scan_min[i].setValue(float(min_vals[i]))
                if hasattr(self, "_scan_max") and i < len(self._scan_max) and i < len(max_vals):
                    self._scan_max[i].setValue(float(max_vals[i]))
            if hasattr(self, "_scan_interval") and interval is not None:
                self._scan_interval.setValue(float(interval))

        def _refresh_presets():
            preset_combo.clear()
            preset_combo.addItems(self.control_api.list_experiment_presets())

        def _apply_preset(exp_data: dict):
            name_input.setText(exp_data.get("name", ""))
            tfr_val = float(exp_data.get("tfr", 0) or 0)
            frr_val = float(exp_data.get("frr", 0) or 0)
            lipid_fr = (tfr_val / frr_val) if frr_val else 0.0
            buffer_fr = max(tfr_val - lipid_fr, 0.0)
            lipid_flow_spin.setValue(float(lipid_fr))
            buffer_flow_spin.setValue(float(buffer_fr))
            vol_spin.setValue(float(exp_data.get("volume", 0) or 0))
            rep_spin.setValue(int(exp_data.get("repeats", 1) or 1))
            line3_rate = float(exp_data.get("line3_constant_flow_rate", 0.0) or 0.0) if line3_feature_enabled else 0.0
            line3_const_rate.setValue(line3_rate)
            _sync_line3_constant_mode()
            # Apply lipid stocks
            lipid_stocks = exp_data.get("lipid_stocks") or []
            lipid_change_guard["suspend"] = True
            try:
                for i, cb in enumerate(lipid_combos):
                    cb.setCurrentIndex(0)
                    if i < len(lipid_stocks):
                        name = lipid_stocks[i].get("name")
                        if name:
                            for j in range(cb.count()):
                                if cb.itemData(j) == name:
                                    cb.setCurrentIndex(j)
                                    break
            finally:
                lipid_change_guard["suspend"] = False
            lipid_change_guard["prev_indices"] = [cb.currentIndex() for cb in lipid_combos]
            lipid_change_guard["prev_count"] = sum(1 for cb in lipid_combos if cb.currentData())

            # Screen space
            mode = exp_data.get("screen_space_mode", "Manual")
            space_combo.setCurrentText(mode)
            self._update_screen_space(mode)

            params = exp_data.get("screen_space_params", {}) or {}
            if mode == "Load":
                if hasattr(self, "_load_path") and params.get("path"):
                    self._load_path.setText(params.get("path"))
            elif mode == "Scan":
                min_vals = params.get("min_vals", [])
                max_vals = params.get("max_vals", [])
                interval = params.get("interval", None)
                for i in range(3):
                    if hasattr(self, "_scan_min") and i < len(self._scan_min) and i < len(min_vals):
                        self._scan_min[i].setValue(float(min_vals[i]))
                    if hasattr(self, "_scan_max") and i < len(self._scan_max) and i < len(max_vals):
                        self._scan_max[i].setValue(float(max_vals[i]))
                if hasattr(self, "_scan_interval") and interval is not None:
                    self._scan_interval.setValue(float(interval))
            table_comps = params.get("compositions", exp_data.get("compositions", [])) or []
            self._composition_table_data = [self._normalize_comp(c) for c in table_comps]
            if hasattr(self, "_manual_count_label"):
                self._manual_count_label.setText(f"Compositions: {len(self._composition_table_data)}")
            self._refresh_composition_preview_panel()

        def _save_preset():
            name = preset_name.text().strip()
            if not name:
                QMessageBox.warning(dlg, "Name Required", "Enter a preset name to save.")
                return
            if name in self.control_api.list_experiment_presets():
                resp = QMessageBox.question(
                    dlg,
                    "Overwrite Preset",
                    f"Preset '{name}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    return

            mode = space_combo.currentText()
            if line3_feature_enabled and line3_const_rate.value() <= 0.0:
                QMessageBox.warning(dlg, "Error", "Set RNA flow rate (> 0) for line 3 constant mode.")
                return
            lipid_fr_val = float(lipid_flow_spin.value())
            buffer_fr_val = float(buffer_flow_spin.value())
            if lipid_fr_val <= 0:
                QMessageBox.warning(dlg, "Error", "Lipid Flow Rate must be > 0.")
                return
            tfr_val = float(lipid_fr_val + buffer_fr_val)
            frr_val = float(tfr_val / lipid_fr_val)
            exp_data = {
                "name": name_input.text().strip() or "Untitled",
                "buffer": {"name": "Buffer", "concentration": 0},
                "lipid_stocks": exp_state["lipid_stocks"],
                "tfr": tfr_val,
                "frr": frr_val,
                "volume": vol_spin.value(),
                "repeats": rep_spin.value(),
                "screen_space_mode": mode,
                "screen_space_params": self._get_screen_space_params(mode),
                "line3_constant_flow_enabled": bool(line3_feature_enabled and line3_const_rate.value() > 0.0),
                "line3_constant_flow_rate": float(line3_const_rate.value() if line3_feature_enabled and line3_const_rate.value() > 0.0 else 0.0),
            }
            ok, err = self.control_api.save_experiment_preset(name, exp_data=exp_data)
            if not ok:
                QMessageBox.warning(dlg, "Save Failed", err or "Failed to save preset.")
                return
            _refresh_presets()
            preset_combo.setCurrentText(name)
            QMessageBox.information(dlg, "Saved", f"Preset '{name}' saved.")

        def _load_preset():
            name = preset_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Select Preset", "Select a preset to load.")
                return
            exp_data = self.control_api.load_experiment_preset(name)
            if not exp_data:
                QMessageBox.warning(dlg, "Not Found", f"Preset '{name}' not found.")
                return
            _apply_preset(exp_data)

        def _delete_preset():
            name = preset_combo.currentText()
            if not name:
                QMessageBox.warning(dlg, "Select Preset", "Select a preset to delete.")
                return
            resp = QMessageBox.question(
                dlg,
                "Delete Preset",
                f"Delete preset '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
            self.control_api.delete_experiment_preset(name)
            _refresh_presets()
            QMessageBox.information(dlg, "Deleted", f"Preset '{name}' deleted.")

        save_preset_btn.clicked.connect(_save_preset)
        load_preset_btn.clicked.connect(_load_preset)
        delete_preset_btn.clicked.connect(_delete_preset)

        save_btn = QPushButton("Save Changes")
        def _save():
            mode = space_combo.currentText()
            if any(abs(sum(comp) - 100) > 1 for comp in getattr(self, "_composition_table_data", [])):
                QMessageBox.warning(
                    dlg,
                    "Composition Warning",
                    "One or more compositions do not sum to 100 (+/-1). The experiment will still be saved.",
                )

            # Gather lipid stocks
            lipid_stocks = []
            selected_names = []
            for cb in lipid_combos:
                name = cb.currentData()
                if not name:
                    continue
                if name in selected_names:
                    QMessageBox.warning(dlg, "Error", f"Lipid '{name}' cannot be used on multiple lines")
                    return
                selected_names.append(name)
                cfg = self.control_api.load_lipid_config(name) or {}
                lipid_stocks.append({
                    "name": name,
                    "concentration": cfg.get("concentration_mM", cfg.get("concentration", "")),
                    "mw": cfg.get("mw", ""),
                    "color": cfg.get("color", "#555555")
                })
            
            if not lipid_stocks:
                QMessageBox.warning(dlg, "Error", "Please select at least one lipid.")
                return

            if line3_feature_enabled and line3_const_rate.value() <= 0.0:
                QMessageBox.warning(dlg, "Error", "Set RNA flow rate (> 0) for line 3 constant mode.")
                return
            lipid_fr_val = float(lipid_flow_spin.value())
            buffer_fr_val = float(buffer_flow_spin.value())
            if lipid_fr_val <= 0:
                QMessageBox.warning(dlg, "Error", "Lipid Flow Rate must be > 0.")
                return
            tfr_val = float(lipid_fr_val + buffer_fr_val)
            frr_val = float(tfr_val / lipid_fr_val)

            exp_data = {
                "name": name_input.text().strip() or "Untitled",
                "buffer": {"name": "Buffer", "concentration": 0},
                "lipid_stocks": lipid_stocks,
                "tfr": tfr_val,
                "frr": frr_val,
                "volume": vol_spin.value(),
                "repeats": rep_spin.value(),
                "screen_space_mode": mode,
                "screen_space_params": self._get_screen_space_params(mode),
                "line3_constant_flow_enabled": bool(line3_feature_enabled and line3_const_rate.value() > 0.0),
                "line3_constant_flow_rate": float(line3_const_rate.value() if line3_feature_enabled and line3_const_rate.value() > 0.0 else 0.0),
            }
            
            ok, err = self.control_api.edit_experiment_in_queue(exp_id, exp_data)
            if ok:
                dlg.accept()
                QMessageBox.information(dlg, "Saved", "Experiment updated successfully.")
            else:
                QMessageBox.warning(dlg, "Error", f"Failed to save: {err}")
        
        save_btn.clicked.connect(_save)
        layout.addWidget(save_btn)

        dlg.exec()

    def _refresh_queue_table(self):
        self.exp_table.clear()
        self.exp_table.setHeaderLabels(["Order", "Log #", "Name", "Lipids", "Est Lines", "# Comp", "Status"])
        queue = self.control_api.get_queue()
        est_map = {}
        try:
            est_map = self.control_api.estimate_queue_line_assignments()
        except Exception:
            est_map = {}
        items = []
        for idx, exp in enumerate(queue):
            order = idx + 1
            lipids = ", ".join([l["name"] for l in exp.lipid_stocks]) if exp.lipid_stocks else "-"
            items.append((order, exp, lipids))

        for order, exp, lipids in sorted(items, key=lambda x: x[0]):
            rec_num = self.control_api.get_experiment_record_number(exp.exp_id)
            rec_text = str(rec_num) if rec_num is not None else "-"
            est = est_map.get(str(exp.exp_id), {}) if isinstance(est_map, dict) else {}
            slot_to_line = est.get("slot_to_line") or []
            slot_perm = est.get("slot_perm_new_to_old") or []
            if slot_to_line:
                parts = []
                for i, ln in enumerate(slot_to_line):
                    if slot_perm and i < len(slot_perm) and slot_perm[i] != i:
                        parts.append(f"S{i+1}*->L{ln}")
                    else:
                        parts.append(f"S{i+1}->L{ln}")
                est_lines_txt = ", ".join(parts)
            else:
                est_lines_txt = "-"
            item = QTreeWidgetItem([str(order), rec_text, exp.name, lipids, est_lines_txt, str(len(exp.compositions)), exp.status])
            item.setData(0, Qt.ItemDataRole.UserRole, exp.exp_id)

            rows = self.control_api.get_experiment_details(exp.exp_id)
            has_unrunnable = any(not row.get("is_runnable", True) for row in rows)
            if has_unrunnable:
                for col in range(7):
                    item.setBackground(col, QColor(255, 100, 100, 80))

            self.exp_table.addTopLevelItem(item)
        self._update_experiment_queue_buttons()
        
        # Update plate visualization with pending/running experiments
        self._update_plate_visualization()

    def _update_plate_visualization(self):
        """Update plate visualization with all queued experiments, including completed."""
        try:
            # Clear all existing wells first to avoid accumulation
            self.output_plate_widget.wells.clear()
            self.output_plate_widget.well_opacity.clear()
            self.output_plate_widget.well_info.clear()
            
            queue = self.control_api.get_queue()
            for exp in queue:
                if exp.status in ("pending", "stopped", "paused", "running", "completed"):
                    # Update each well based on composition and completion status
                    for i, comp in enumerate(exp.compositions):
                        well = exp.output_wells[i]
                        color = self.control_api.get_composition_color(comp, exp.lipid_stocks)
                        # Preview (transparent) only for pending experiments with uncompleted compositions
                        is_completed = i < len(exp.comp_status) and exp.comp_status[i] == "completed"
                        is_preview = exp.status == "pending" and not is_completed
                        # Pass experiment name and composition percentages
                        self.output_plate_widget.set_well_color(
                            well[0], well[1], well[2], color, 
                            is_preview=is_preview,
                            exp_name=exp.name,
                            composition=comp
                        )
            # Redraw the widget
            self.output_plate_widget.update()
        except Exception as e:
            print(f"[GUI] Could not update plate visualization: {e}")

    def _open_experiment_details(self, item, _col):
        exp_id = item.data(0, Qt.ItemDataRole.UserRole)
        rows = self.control_api.get_experiment_details(exp_id)

        dlg = QDialog(self)
        dlg.setWindowTitle("Experiment Details")
        dlg.resize(1400, 600)
        layout = QVBoxLayout(dlg)

        headers = ["Repeat", "Buffer", "Lipids", "Composition", "Buffer FR", "Lipid FRs", "Well", "Status", "Lipid Allocation", "Prep Commands"]
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)

        for r, row in enumerate(rows):
            flow = [float(x) for x in row["flow_rates"]]
            while len(flow) < 4:
                flow.append(0.0)
            comp = row["composition"]
            while len(comp) < 3:
                comp.append(0.0)

            # Repeat column - show which repeat this is out of total
            # Note: exp.repeats is stored in the row data
            total_repeats = rows[0].get('total_repeats', 1) if rows else 1
            repeat_text = f"{row['repeat_num']}/{total_repeats}"
            table.setItem(r, 0, QTableItem(repeat_text))
            
            table.setItem(r, 1, QTableItem(row["buffer"]))
            table.setItem(r, 2, QTableItem(", ".join(row["lipids"])))
            table.setItem(r, 3, QTableItem(f"{comp[0]:.1f}, {comp[1]:.1f}, {comp[2]:.1f}"))
            table.setItem(r, 4, QTableItem(f"{flow[0]:.2f}"))
            
            # Combined lipid FRs
            lipid_frs = f"{flow[1]:.2f}, {flow[2]:.2f}, {flow[3]:.2f}"
            table.setItem(r, 5, QTableItem(lipid_frs))
            
            table.setItem(r, 6, QTableItem(str(row["well"])))
            table.setItem(r, 7, QTableItem(row["status"]))
            
            # Lipid Availability with color
            avail_item = QTableItem(row["lipid_availability"])
            if not row.get("is_runnable", True):
                # Mark unrunnable compositions red
                avail_item.setBackground(QColor(255, 50, 50, 150))
            elif "✗" in row["lipid_availability"]:
                avail_item.setBackground(QColor(255, 100, 100, 120))
            elif "⚠" in row["lipid_availability"]:
                avail_item.setBackground(QColor(255, 200, 100, 120))
            else:
                avail_item.setBackground(QColor(100, 255, 100, 120))
            table.setItem(r, 8, avail_item)
            
            # Prep commands
            detailed_status = row.get("detailed_status", "")
            status_item = QTableItem(detailed_status)
            table.setItem(r, 9, status_item)

        table.resizeColumnsToContents()
        layout.addWidget(table)
        
        # Button layout
        button_layout = QHBoxLayout()
        
        delete_btn = QPushButton("Delete Experiment")
        delete_btn.setStyleSheet("background-color: #ff6b6b; color: white;")
        def _delete():
            reply = QMessageBox.warning(
                dlg, 
                "Delete Experiment", 
                f"Are you sure you want to delete this experiment?\n\nExp ID: {exp_id}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.control_api.delete_experiment(exp_id)
                dlg.accept()
                self._refresh_queue_table()
                QMessageBox.information(dlg, "Deleted", "Experiment deleted successfully.")
        
        delete_btn.clicked.connect(_delete)
        button_layout.addWidget(delete_btn)

        repeat_btn = QPushButton("Repeat Experiment")
        repeat_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        def _repeat():
            reply = QMessageBox.question(
                dlg,
                "Repeat Experiment",
                "Add a duplicate of this experiment to the queue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.control_api.repeat_experiment(exp_id)
                self._refresh_queue_table()
                QMessageBox.information(dlg, "Added", "Experiment duplicated in queue.")
        repeat_btn.clicked.connect(_repeat)
        button_layout.addWidget(repeat_btn)
        
        edit_btn = QPushButton("Edit Experiment")
        edit_btn.setStyleSheet("background-color: #2196F3; color: white;")
        def _edit():
            exp = self.control_api.get_experiment(exp_id)
            if not exp:
                QMessageBox.warning(dlg, "Error", "Experiment not found")
                return
            if exp.status != "pending":
                QMessageBox.warning(dlg, "Error", "Can only edit pending experiments")
                return
            
            # Open edit dialog with current values
            self._open_experiment_edit_dialog(exp_id, exp)
            dlg.accept()
            self._refresh_queue_table()
        edit_btn.clicked.connect(_edit)
        button_layout.addWidget(edit_btn)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        button_layout.addWidget(close_btn)
        
        layout.addLayout(button_layout)
        
        dlg.exec()

    def _update_status(self):
        """Pull status from backend and update GUI."""
        try:
            status = self.control_api.get_status()
            self._sync_intake_visualization()

            # Update connection indicators
            for device, connected in status["connections"].items():
                name = device.capitalize()
                if name in self.conn_buttons:
                    self.conn_buttons[name].setStyleSheet("background-color: #7CFC90;" if connected else "")
                    self._conn_state[name] = bool(connected)
            self._update_start_enabled()

            # Refresh queue table and plot colors when experiment changes
            current_exp = status.get("current_experiment")
            if current_exp != getattr(self, "_last_current_exp", None):
                self._last_current_exp = current_exp
                self._refresh_queue_table()
                self._update_plot_colors(current_exp)
                # Also refresh well colors when new experiment starts
                self._sync_plate_visualization()

            # Update nozzle positions
            # ...existing code...

            # Update plots
            flow_data = status["flow_data"]
            if flow_data["time"]:
                for i in range(4):
                    self.flow_curves[i].setData(flow_data["time"], flow_data["flows"][i])
                    self.pressure_curves_set[i].setData(flow_data["time"], flow_data["pressures_set"][i])
                    self.pressure_curves_act[i].setData(flow_data["time"], flow_data["pressures_act"][i])
                extra_flow = flow_data.get("extra_flow") or []
                extra_p_set = flow_data.get("extra_pressure_set") or []
                extra_p_act = flow_data.get("extra_pressure_act") or []
                if extra_flow and any(v is not None for v in extra_flow):
                    def _plot_series(vals):
                        n = len(flow_data["time"])
                        padded = list(vals[:n]) + [None] * max(0, n - len(vals))
                        return [np.nan if v is None else float(v) for v in padded]
                    self.extra_flow_curve.setData(flow_data["time"], _plot_series(extra_flow))
                    self.extra_pressure_curve_set.setData(flow_data["time"], _plot_series(extra_p_set))
                    self.extra_pressure_curve_act.setData(flow_data["time"], _plot_series(extra_p_act))
                else:
                    self.extra_flow_curve.clear()
                    self.extra_pressure_curve_set.clear()
                    self.extra_pressure_curve_act.clear()

            live_flows = status.get("live_flows", {}) or {}
            def _fmt(v):
                return "-" if v is None else f"{float(v):.1f}"
            label_text = (
                f"Ch1: {_fmt(live_flows.get('ch1'))} | "
                f"Ch2: {_fmt(live_flows.get('ch2'))} | "
                f"Ch3: {_fmt(live_flows.get('ch3'))} | "
                f"Ch4: {_fmt(live_flows.get('ch4'))}"
            )
            if live_flows.get("extra_enabled"):
                label_text += f" | RNA: {_fmt(live_flows.get('extra'))}"
            self.flow_values_label.setText(label_text)

            # Apply fixed plot ranges based on setpoints (if provided)
            plot_ranges = status.get("plot_ranges", {})
            flow_ylim = plot_ranges.get("flow_ylim")
            pressure_ylim = plot_ranges.get("pressure_ylim")
            flow_mode = "lipids"
            try:
                flow_mode = str(self.flow_y_range_mode_combo.currentData() or "lipids")
            except Exception:
                flow_mode = "lipids"
            if flow_mode == "lipids":
                if flow_ylim and len(flow_ylim) == 2:
                    self.flow_plot.setYRange(flow_ylim[0], flow_ylim[1], padding=0)
                elif flow_data["time"]:
                    lipid_flows = flow_data["flows"][1] + flow_data["flows"][2] + flow_data["flows"][3]
                    if lipid_flows:
                        max_lipid_flow = max(lipid_flows)
                        self.flow_plot.setYRange(0, max(max_lipid_flow * 1.1, 1.0), padding=0)
            else:
                if flow_data["time"]:
                    all_flows = (
                        flow_data["flows"][0]
                        + flow_data["flows"][1]
                        + flow_data["flows"][2]
                        + flow_data["flows"][3]
                        + [v for v in (flow_data.get("extra_flow") or []) if v is not None]
                    )
                    if all_flows:
                        ymin = min(all_flows)
                        ymax = max(all_flows)
                        if ymax <= ymin:
                            ymax = ymin + 1.0
                        pad = max((ymax - ymin) * 0.1, 1.0)
                        self.flow_plot.setYRange(ymin - pad, ymax + pad, padding=0)
            if pressure_ylim and len(pressure_ylim) == 2:
                self.pressure_plot.setYRange(pressure_ylim[0], pressure_ylim[1], padding=0)

            # Update collection marker lines (dotted)
            markers = flow_data.get("collection_markers", [])
            if markers != self._last_collection_markers:
                # Clear old markers
                for line in self.collection_lines_flow:
                    self.flow_plot.removeItem(line)
                for line in self.collection_lines_pressure:
                    self.pressure_plot.removeItem(line)
                self.collection_lines_flow = []
                self.collection_lines_pressure = []

                marker_pen = pg.mkPen(color=(200, 200, 200), width=1, style=Qt.PenStyle.DotLine)
                for t in markers:
                    line_flow = pg.InfiniteLine(pos=t, angle=90, pen=marker_pen)
                    line_pressure = pg.InfiniteLine(pos=t, angle=90, pen=marker_pen)
                    self.flow_plot.addItem(line_flow)
                    self.pressure_plot.addItem(line_pressure)
                    self.collection_lines_flow.append(line_flow)
                    self.collection_lines_pressure.append(line_pressure)

                self._last_collection_markers = list(markers)

            # Update progress
            if status["target_volume"] > 0:
                progress = int((status["collected_volume"] / status["target_volume"]) * 100)
                self.progress_bar.setValue(progress)

            # Update status text
            ui_status = status.get("ui_status")
            if ui_status:
                self.status_label.setText(f"Status: {ui_status}")
            else:
                self.status_label.setText(f"Status: {status['microfluidic_state']}")

            queue_finished_status = "Queue finished - all pressures stopped"
            queue_finished_now = (ui_status == queue_finished_status and current_exp is None)
            if queue_finished_now and not self._queue_finished_cleanup_prompted:
                self._queue_finished_cleanup_prompted = True
                QTimer.singleShot(0, self._open_clean_all_dialog)
            elif not queue_finished_now:
                self._queue_finished_cleanup_prompted = False

            # Per-line status (simultaneous line view)
            line_status = status.get("line_status") or {}
            l1 = str(line_status.get("1", "Idle"))
            l2 = str(line_status.get("2", "Idle"))
            l3 = str(line_status.get("3", "Idle"))
            self.line_status_label.setText(f"L1: {l1} | L2: {l2} | L3: {l3}")

            # Update lipid line info (name + expelled volume)
            try:
                line_states = self.control_api.get_lipid_line_states()
            except Exception:
                line_states = {}

            for i in range(3):
                name_lbl, vol_lbl = self.lipid_stock_labels[i]
                line_state = line_states.get(i + 1, {})
                lipid_name = line_state.get("lipid_name") or "-"
                remaining = float(line_state.get("remaining_volume", 0) or 0)
                loaded_vol = float(line_state.get("loaded_volume", 450) or 450)
                expelled = max(0.0, loaded_vol - remaining) if lipid_name != "-" else 0.0
                name_lbl.setText(f"Lipid {i+1}: {lipid_name}")
                vol_lbl.setText(f"{expelled:.1f}/{loaded_vol:.0f}µL")

            # Depletion error popup (auto-skip after 5 mins)
            err = status.get("current_error")
            if err and err != self._last_error:
                self._last_error = err
                m = re.search(r"Lipid depleted: (.+)", err)
                lipid = m.group(1) if m else None
                title = "Lipid Depleted" if lipid else "System Warning"
                QMessageBox.warning(self, title, err)
                if lipid:
                    QTimer.singleShot(5 * 60 * 1000, lambda: self.control_api.skip_lipid_experiments(lipid))

            # Recoverable Dobot reconnect popup
            prompt = status.get("recovery_prompt")
            if prompt != self._last_recovery_prompt:
                self._last_recovery_prompt = prompt
                if prompt and prompt.get("type") == "dobot_reconnect":
                    self._show_dobot_recovery_popup(prompt)
        except Exception as e:
            now = time.monotonic()
            if (now - self._last_status_error_t) > 2.0:
                self._last_status_error_t = now
                print(f"[GUI] _update_status error: {e}")
                print(traceback.format_exc(), end="")

    def _show_dobot_recovery_popup(self, prompt: dict):
        if self._recovery_popup_open:
            return
        self._recovery_popup_open = True
        try:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowTitle("Dobot Reconnect Required")
            msg.setText(prompt.get("message", "Reconnect Dobot, then continue."))
            line = prompt.get("line")
            lipid = prompt.get("lipid_name")
            if line:
                info = f"Interrupted during line {line}"
                if lipid:
                    info += f" ({lipid})"
                msg.setInformativeText(info)
            reconnect_btn = msg.addButton("Reconnect Dobot", QMessageBox.ButtonRole.ActionRole)
            continue_btn = msg.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            while True:
                msg.exec()
                clicked = msg.clickedButton()
                if clicked == reconnect_btn:
                    self._open_dobot_popup()
                    continue
                if clicked == continue_btn:
                    ok, err = self.control_api.continue_after_dobot_recovery()
                    if ok:
                        break
                    QMessageBox.warning(self, "Cannot Continue", err or "Reconnect Dobot first.")
                    continue
                break
        finally:
            self._recovery_popup_open = False

    def _update_plot_colors(self, exp_id):
        """Update flow/pressure plot colors based on buffer/lipid colors."""
        if exp_id:
            colors = self.control_api.get_experiment_plot_colors(exp_id)
            qt_colors = [QColor(c) for c in colors]
        else:
            qt_colors = [QColor(*c) for c in self._default_plot_colors]

        for i in range(4):
            self.flow_curves[i].setPen(pg.mkPen(color=qt_colors[i], width=2))
            self.pressure_curves_set[i].setPen(pg.mkPen(color=qt_colors[i], width=2, style=Qt.PenStyle.SolidLine))
            self.pressure_curves_act[i].setPen(pg.mkPen(color=qt_colors[i], width=2, style=Qt.PenStyle.DashLine))
        extra_color = QColor(80, 220, 140)
        self.extra_flow_curve.setPen(pg.mkPen(color=extra_color, width=2, style=Qt.PenStyle.DotLine))
        self.extra_pressure_curve_set.setPen(pg.mkPen(color=extra_color, width=2, style=Qt.PenStyle.DotLine))
        self.extra_pressure_curve_act.setPen(pg.mkPen(color=extra_color, width=2, style=Qt.PenStyle.DashLine))

    def _sync_plate_visualization(self):
        """Sync well visualization with queue status (mark completed wells as opaque)."""
        try:
            queue = self.control_api.get_queue()
            for exp in queue:
                # Update each well based on composition and completion status
                for i, comp in enumerate(exp.compositions):
                    well = exp.output_wells[i]
                    color = self.control_api.get_composition_color(comp, exp.lipid_stocks)
                    is_completed = i < len(exp.comp_status) and exp.comp_status[i] == "completed"
                    is_preview = not is_completed
                    self.output_plate_widget.set_well_color(well[0], well[1], well[2], color, is_preview=is_preview)
        except Exception as e:
            print(f"[GUI] Could not sync plate visualization: {e}")

    def _switch_output_plate(self, plate_idx):
        self.output_plate_widget.set_plate(plate_idx)
        self._update_plate_button_styles()
        plate, row, col = self.output_plate_widget.selected_well
        self.control_api.set_start_well(plate, row, col)

    def _update_plate_button_styles(self):
        for idx, btn in enumerate(self.plate_buttons, start=1):
            if idx == self.output_plate_widget.current_plate:
                btn.setStyleSheet("font-size: 10px; background-color: #7CFC90;")
            else:
                btn.setStyleSheet("font-size: 10px;")

    def _prompt_resume_checkpoint(self):
        if not self.control_api.has_checkpoint():
            return
        reply = QMessageBox.question(
            self,
            "Resume Interrupted Run",
            "A previous run was detected. Resume from checkpoint?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.control_api.resume_from_checkpoint()
            self._refresh_queue_table()
            self._restore_intake_visualization()
            self._restore_buffer_selection_from_queue()
        else:
            self.control_api.clear_checkpoint()
            self._refresh_queue_table()

    def _open_clean_all_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Clean All Parameters")
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Flush volume (0-500 µL):"))
        vol_input = QSpinBox()
        vol_input.setRange(0, 500)
        vol_input.setValue(200)
        layout.addWidget(vol_input)

        layout.addWidget(QLabel("Wash cycles:"))
        cycles_input = QSpinBox()
        cycles_input.setRange(1, 10)
        cycles_input.setValue(1)
        layout.addWidget(cycles_input)

        layout.addWidget(QLabel("Lines to clean:"))
        lines_row = QHBoxLayout()
        clean_line_checks = {}
        for ln in (1, 2, 3):
            cb = QCheckBox(f"L{ln}")
            cb.setChecked(True)
            clean_line_checks[ln] = cb
            lines_row.addWidget(cb)
        lines_row.addStretch()
        layout.addLayout(lines_row)

        flush_chip_chk = QCheckBox("Flush through chip")
        flush_chip_chk.setChecked(False)
        layout.addWidget(flush_chip_chk)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Start")
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        def _start():
            flush_volume = vol_input.value()
            wash_cycles = cycles_input.value()
            selected_lines = [ln for ln, cb in clean_line_checks.items() if cb.isChecked()]
            flush_through_chip = flush_chip_chk.isChecked()
            ok, err = self.control_api.clean_all(
                flush_volume=flush_volume,
                flush_through_chip=flush_through_chip,
                wash_cycles=wash_cycles,
                lines=selected_lines,
            )
            if not ok:
                QMessageBox.warning(self, "Cannot clean", err or "Clean All not allowed right now.")
            else:
                dlg.accept()

        ok_btn.clicked.connect(_start)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _clean_all(self):
        self._open_clean_all_dialog()

    def _open_lipid_library_popup(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Lipid Library")
        dlg.resize(900, 560)
        layout = QVBoxLayout(dlg)

        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["Name", "Code", "Concentration", "Units", "MW", "Color"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(table)

        form = QGridLayout()
        form.addWidget(QLabel("Name"), 0, 0)
        name_input = QLineEdit()
        form.addWidget(name_input, 0, 1)

        form.addWidget(QLabel("Code"), 0, 2)
        code_input = QLineEdit()
        code_input.setPlaceholderText("e.g. DOPC01")
        form.addWidget(code_input, 0, 3)

        form.addWidget(QLabel("Concentration"), 1, 0)
        conc_input = QLineEdit()
        form.addWidget(conc_input, 1, 1)

        form.addWidget(QLabel("Units"), 1, 2)
        units_combo = QComboBox()
        units_combo.addItems(["mM", "mg/ml"])
        form.addWidget(units_combo, 1, 3)

        form.addWidget(QLabel("MW"), 2, 0)
        mw_input = QLineEdit()
        form.addWidget(mw_input, 2, 1)

        form.addWidget(QLabel("Color"), 2, 2)
        color_btn = QPushButton("Select Color")
        color_btn.setStyleSheet("background-color: #555555;")
        color_btn.clicked.connect(lambda: self._select_lipid_color(color_btn))
        form.addWidget(color_btn, 2, 3)

        layout.addLayout(form)

        info_lbl = QLabel("Note: Existing lipid names are locked in this editor. Use Add New for new names.")
        info_lbl.setStyleSheet("font-size: 10px; color: #BBBBBB;")
        layout.addWidget(info_lbl)

        dlg._selected_name = None

        def _toggle_mw():
            mw_input.setEnabled(units_combo.currentText() == "mg/ml")
        units_combo.currentTextChanged.connect(_toggle_mw)

        def _new_entry():
            dlg._selected_name = None
            name_input.clear()
            code_input.clear()
            conc_input.clear()
            mw_input.clear()
            units_combo.setCurrentText("mM")
            color_btn.setStyleSheet("background-color: #555555;")
            if hasattr(color_btn, "color"):
                delattr(color_btn, "color")
            name_input.setEnabled(True)
            table.clearSelection()
            _toggle_mw()

        def _refresh_table(select_name=None):
            names = self.control_api.get_lipid_configs()
            rows = []
            for name in names:
                cfg = self.control_api.load_lipid_config(name) or {}
                rows.append((name, cfg))
            rows.sort(key=lambda x: x[0].lower())
            table.setRowCount(len(rows))
            selected_row = None
            for r, (name, cfg) in enumerate(rows):
                color_hex = cfg.get("color", "#555555")
                vals = [
                    name,
                    str(cfg.get("lipid_code", "")),
                    str(cfg.get("concentration", "")),
                    str(cfg.get("units", "mM")),
                    str(cfg.get("mw", "")),
                    color_hex,
                ]
                for c, v in enumerate(vals):
                    item = QTableItem(v)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    table.setItem(r, c, item)
                if select_name and name == select_name:
                    selected_row = r
            if selected_row is not None:
                table.selectRow(selected_row)

        def _load_selected():
            items = table.selectedItems()
            if not items:
                return
            row = items[0].row()
            sel_name = table.item(row, 0).text()
            cfg = self.control_api.load_lipid_config(sel_name) or {}
            dlg._selected_name = sel_name
            name_input.setText(sel_name)
            name_input.setEnabled(False)
            code_input.setText(str(cfg.get("lipid_code", "")).upper())
            conc_input.setText(str(cfg.get("concentration", "")))
            units_combo.setCurrentText(str(cfg.get("units", "mM")))
            mw_input.setText(str(cfg.get("mw", "")))
            color_hex = cfg.get("color", "#555555")
            color_btn.setStyleSheet(f"background-color: {color_hex};")
            color_btn.color = QColor(color_hex)
            _toggle_mw()

        def _save_entry():
            selected_name = dlg._selected_name
            name = name_input.text().strip()
            code = code_input.text().strip().upper()
            conc = conc_input.text().strip()
            units = units_combo.currentText().strip()
            mw = mw_input.text().strip()
            if not name:
                QMessageBox.warning(dlg, "Error", "Lipid name is required.")
                return
            if not conc:
                QMessageBox.warning(dlg, "Error", "Concentration is required.")
                return
            if units == "mg/ml" and not mw:
                QMessageBox.warning(dlg, "Error", "MW is required when units are mg/ml.")
                return
            if code and not re.fullmatch(r"[A-Z0-9]+", code):
                QMessageBox.warning(dlg, "Error", "Code must be uppercase letters/numbers only, no spaces.")
                return
            try:
                conc_val = float(conc)
                conc_mM = conc_val if units == "mM" else (conc_val * 1000.0 / float(mw))
            except Exception:
                QMessageBox.warning(dlg, "Error", "Invalid concentration/MW value.")
                return

            color_hex = color_btn.color.name() if hasattr(color_btn, "color") else "#555555"
            save_name = selected_name or name
            try:
                self.control_api.save_lipid_config(
                    save_name,
                    {
                        "concentration": conc,
                        "units": units,
                        "mw": mw,
                        "concentration_mM": conc_mM,
                        "color": color_hex,
                        "lipid_code": code,
                    },
                )
            except Exception as e:
                QMessageBox.warning(dlg, "Error", str(e))
                return

            self._restore_intake_visualization()
            _refresh_table(select_name=save_name)
            dlg._selected_name = save_name
            name_input.setText(save_name)
            name_input.setEnabled(False)

        def _delete_selected():
            items = table.selectedItems()
            if not items:
                QMessageBox.warning(dlg, "Error", "Select a lipid to delete.")
                return
            row = items[0].row()
            sel_name = table.item(row, 0).text()
            resp = QMessageBox.question(
                dlg,
                "Delete Lipid",
                f"Delete lipid '{sel_name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
            self.control_api.delete_lipid_config(sel_name)
            _new_entry()
            _refresh_table()
            self._restore_intake_visualization()

        table.itemSelectionChanged.connect(_load_selected)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("Add New")
        new_btn.clicked.connect(_new_entry)
        btn_row.addWidget(new_btn)

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(_save_entry)
        btn_row.addWidget(save_btn)

        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(_delete_selected)
        btn_row.addWidget(del_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        _new_entry()
        _refresh_table()
        dlg.exec()

    def _restore_buffer_selection_from_queue(self):
        """Restore buffer selection based on the next experiment in queue."""
        try:
            queue = self.control_api.get_queue()
            next_exp = next((e for e in queue if e.status in ("pending", "stopped", "paused", "running")), None)
            if not next_exp or not getattr(next_exp, "buffer", None):
                return
            buffer_name = next_exp.buffer.get("name") if isinstance(next_exp.buffer, dict) else None
            if not buffer_name:
                return
            self.control_api.set_buffer_selected(buffer_name)
            self.buffer_btn.setText(f"Buffer: {buffer_name}")
            self.buffer_btn.setStyleSheet("background-color: #555555; color: white;")
        except Exception as e:
            print(f"[GUI] Could not restore buffer selection: {e}")

    def _on_start_well_selected(self, well_pos):
        """Handle plate click: selected experiment start-well or global queue start-well."""
        plate, row, col = well_pos
        row_label = chr(64 + row)  # A=65, B=66, etc.
        exp_id = getattr(self, "_selected_start_exp_id", None)
        if exp_id:
            ok, err = self.control_api.set_experiment_start_well(exp_id, plate, row, col)
            if not ok:
                QMessageBox.warning(self, "Start Well Update Failed", err or "Could not update experiment start well.")
                return
            exp = self.control_api.get_experiment(exp_id)
            exp_name = getattr(exp, "name", exp_id) if exp else exp_id
            self.start_well_label.setText(f"Start Well ({exp_name}): P{plate} {row_label}{col}")
            self._refresh_queue_table()
            self._sync_plate_visualization()
            self.output_plate_widget.update()
            return

        # Fallback legacy behavior: global start well for pending queue.
        self.control_api.set_start_well(plate, row, col)
        self._recalculate_pending_positions(plate, row, col)
        self.output_plate_widget.update()
        self.start_well_label.setText(f"Start Well: P{plate} {row_label}{col}")
        self._refresh_queue_table()

    def _recalculate_pending_positions(self, start_plate, start_row, start_col):
        """Recalculate output well positions for all pending experiments based on new start position."""
        try:
            ok, err = self.control_api.recalculate_pending_positions(start_plate, start_row, start_col)
            if not ok:
                QMessageBox.warning(self, "Recalculate Failed", err or "Could not recalculate positions.")
                return

            # Refresh visualization from updated experiment data
            self._update_plate_visualization()
        except Exception as e:
            print(f"[GUI] Could not recalculate positions: {e}")

    def _start(self):
        ok, err = self.control_api.start()
        if not ok:
            QMessageBox.warning(self, "Cannot start", err or "Start conditions not met.")
            return
        self.stop_btn.setEnabled(True)

    def _stop(self):
        self.control_api.stop()

    def _pause(self):
        self.control_api.pause()

    def closeEvent(self, event):
        try:
            self.control_api.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

    def _skip(self):
        self.control_api.skip()

    def _open_admin_tools(self):
        """Open admin tools popup."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Admin Tools")
        dlg.setMinimumWidth(820)
        layout = QVBoxLayout(dlg)

        # Warning label
        warning_label = QLabel("⚠️ Advanced Controls - Use with caution")
        warning_label.setStyleSheet("color: #FFA500; font-weight: bold; font-size: 14px;")
        layout.addWidget(warning_label)

        # Two-column body to avoid an overly long single column
        body_layout = QHBoxLayout()
        left_col = QVBoxLayout()
        right_col = QVBoxLayout()
        body_layout.addLayout(left_col, 1)
        body_layout.addLayout(right_col, 1)
        layout.addLayout(body_layout)

        # Start without loading button
        start_no_load_btn = QPushButton("Start Without Loading")
        start_no_load_btn.setStyleSheet("background-color: #FF6B6B; font-size: 12px; padding: 10px;")
        start_no_load_btn.clicked.connect(lambda: self._start_without_loading(dlg))
        left_col.addWidget(start_no_load_btn)

        # Description
        desc_label = QLabel(
            "Start Without Loading:\n"
            "Runs the next experiment assuming all lipids\n"
            "are already loaded. Skips robot loading sequence."
        )
        desc_label.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        left_col.addWidget(desc_label)

        # Test line-switch protocol button
        switch_test_btn = QPushButton("Run Line Switch Protocol")
        switch_test_btn.setStyleSheet("background-color: #4ECDC4; font-size: 12px; padding: 10px;")
        switch_test_btn.clicked.connect(self._admin_test_line_switch_protocol)
        left_col.addWidget(switch_test_btn)

        switch_desc = QLabel(
            "Select lines to switch for next pending experiment.\n"
            "Changed lines clean in parallel, then selected lines load sequentially."
        )
        switch_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        left_col.addWidget(switch_desc)

        # Multi-plate calibration button
        calib_btn = QPushButton("Multi-Plate Calibration")
        calib_btn.setStyleSheet("background-color: #4ECDC4; font-size: 12px; padding: 10px;")
        calib_btn.clicked.connect(self._open_multiplate_calibration)
        left_col.addWidget(calib_btn)

        calib_desc = QLabel(
            "Calibrate the first well position for output plates 1-6.\n"
            "Home the stage, jog with arrows, then confirm to save."
        )
        calib_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        left_col.addWidget(calib_desc)

        dobot_ctrl_btn = QPushButton("Dobot Control")
        dobot_ctrl_btn.setStyleSheet("background-color: #4ECDC4; font-size: 12px; padding: 10px;")
        dobot_ctrl_btn.clicked.connect(self._open_dobot_control_dialog)
        left_col.addWidget(dobot_ctrl_btn)

        dobot_ctrl_desc = QLabel(
            "Open Dobot controls:\n"
            "intake hover, manual jog,\n"
            "and gripper on/off."
        )
        dobot_ctrl_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        left_col.addWidget(dobot_ctrl_desc)

        remove_stoppers_btn = QPushButton("Remove Stoppers")
        remove_stoppers_btn.setStyleSheet("background-color: #4ECDC4; font-size: 12px; padding: 10px;")
        remove_stoppers_btn.clicked.connect(self._open_remove_stopper_dialog)
        left_col.addWidget(remove_stoppers_btn)

        remove_stoppers_desc = QLabel(
            "Pick stopper from selected intake well,\n"
            "drop at disposal point,\n"
            "then return home."
        )
        remove_stoppers_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        left_col.addWidget(remove_stoppers_desc)

        # Plate movement test section
        move_group = QGroupBox("Plate Movement Test")
        move_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        move_layout = QVBoxLayout()

        current_well_label = QLabel("Current well: -")
        current_well_label.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        move_layout.addWidget(current_well_label)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("Plate:"))
        plate_combo = QComboBox()
        plate_combo.addItems([str(i) for i in range(1, 7)])
        input_row.addWidget(plate_combo)
        input_row.addWidget(QLabel("Row:"))
        row_combo = QComboBox()
        row_combo.addItems([chr(65 + i) for i in range(8)])
        input_row.addWidget(row_combo)
        input_row.addWidget(QLabel("Col:"))
        col_spin = QSpinBox()
        col_spin.setRange(1, 12)
        col_spin.setValue(1)
        input_row.addWidget(col_spin)
        input_row.addStretch()
        move_layout.addLayout(input_row)

        move_btn = QPushButton("Move to Well")
        move_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        move_layout.addWidget(move_btn)

        sweep_btn = QPushButton("Well-to-Well Movement")
        sweep_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        sweep_btn.clicked.connect(self._open_well_to_well_test_dialog)
        move_layout.addWidget(sweep_btn)

        random_dobot_btn = QPushButton("Move Random Dobot")
        random_dobot_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        random_dobot_btn.clicked.connect(self._open_random_dobot_dialog)
        move_layout.addWidget(random_dobot_btn)

        home_btn = QPushButton("Home to Plate")
        home_btn.setStyleSheet("background-color: #7CFC90; font-size: 11px; padding: 8px;")
        move_layout.addWidget(home_btn)

        move_group.setLayout(move_layout)
        right_col.addWidget(move_group)

        start_well = self.control_api.get_start_well()
        default_plate = 1
        if isinstance(start_well, (list, tuple)) and len(start_well) == 3:
            try:
                default_plate = int(start_well[0])
            except Exception:
                default_plate = 1
        plate_combo.setCurrentIndex(max(0, min(default_plate - 1, plate_combo.count() - 1)))
        row_combo.setCurrentIndex(0)
        col_spin.setValue(1)

        def _update_current_well_label():
            status = self.control_api.get_status()
            well = status.get("current_well")
            plate = None
            row = None
            col = None
            if isinstance(well, (list, tuple)):
                if len(well) == 3:
                    plate, row, col = well
                elif len(well) == 2:
                    row, col = well
            if plate is None:
                try:
                    plate = int(plate_combo.currentText())
                except Exception:
                    plate = 1
            if row is None or col is None:
                current_well_label.setText("Current well: Unknown")
                return
            try:
                row_int = int(row)
                col_int = int(col)
            except Exception:
                current_well_label.setText("Current well: Unknown")
                return
            row_label = chr(64 + row_int) if 1 <= row_int <= 8 else str(row_int)
            current_well_label.setText(f"Current well: P{plate} {row_label}{col_int}")

        def _home_stage():
            plate = int(plate_combo.currentText())
            def _do_home():
                ok, err = self.control_api.admin_home_stage_to_plate(plate)
                if not ok:
                    QTimer.singleShot(
                        0, 
                        lambda: QMessageBox.warning(self, "Home Failed", err or "Failed to home stage.")
                    )
            threading.Thread(target=_do_home, daemon=True).start()

        def _move_to_well():
            plate = int(plate_combo.currentText())
            row = row_combo.currentIndex() + 1
            col = int(col_spin.value())
            def _do_move():
                ok, err = self.control_api.admin_move_to_well(plate, row, col, rehome=False)
                if not ok:
                    QTimer.singleShot(
                        0, 
                        lambda: QMessageBox.warning(self, "Move Failed", err or "Failed to move stage.")
                    )
            threading.Thread(target=_do_move, daemon=True).start()

        move_btn.clicked.connect(_move_to_well)
        home_btn.clicked.connect(_home_stage)
        plate_combo.currentIndexChanged.connect(_update_current_well_label)

        move_timer = QTimer(dlg)
        move_timer.timeout.connect(_update_current_well_label)
        move_timer.start(500)
        _update_current_well_label()

        # Separator
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("background-color: #555555;")
        left_col.addWidget(separator)

        # Manual Servo Control Section
        servo_group = QGroupBox("Manual Rotary Servo Control")
        servo_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        servo_layout = QVBoxLayout()

        # Servo selection
        servo_select_layout = QHBoxLayout()
        servo_select_layout.addWidget(QLabel("Servo (1-9):"))
        servo_combo = QComboBox()
        servo_combo.addItems([str(i) for i in range(1, 10)])
        servo_select_layout.addWidget(servo_combo)
        servo_select_layout.addStretch()
        servo_layout.addLayout(servo_select_layout)

        # Angle selection
        angle_select_layout = QHBoxLayout()
        angle_select_layout.addWidget(QLabel("Position:"))
        angle_combo = QComboBox()
        angle_combo.addItems(["40° (Close to Dobot/Chip)", "80° (Open to Both)", "125° (Close to Sensors/Waste)"])
        angle_select_layout.addWidget(angle_combo)
        servo_layout.addLayout(angle_select_layout)

        # Set servo button
        set_servo_btn = QPushButton("Set Servo Position")
        set_servo_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        set_servo_btn.clicked.connect(lambda: self._set_manual_servo_position(servo_combo, angle_combo))
        servo_layout.addWidget(set_servo_btn)

        servo_group.setLayout(servo_layout)
        left_col.addWidget(servo_group)

        # Manual Dobot Valve Control Section
        valve_group = QGroupBox("Manual Dobot Valve Control")
        valve_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        valve_layout = QVBoxLayout()

        valve_row = QHBoxLayout()
        valve_row.addWidget(QLabel("Line:"))
        valve_line_combo = QComboBox()
        valve_line_combo.addItems([str(i) for i in range(1, 4)])
        valve_row.addWidget(valve_line_combo)
        valve_row.addWidget(QLabel("State:"))
        valve_state_combo = QComboBox()
        valve_state_combo.addItems(["On", "Off"])
        valve_row.addWidget(valve_state_combo)
        valve_row.addStretch()
        valve_layout.addLayout(valve_row)

        set_valve_btn = QPushButton("Set Dobot Valve")
        set_valve_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        set_valve_btn.clicked.connect(
            lambda: self._set_manual_dobot_valve(valve_line_combo, valve_state_combo)
        )
        valve_layout.addWidget(set_valve_btn)

        valve_group.setLayout(valve_layout)
        left_col.addWidget(valve_group)

        # Manual Channel Pressure Control Section
        pressure_group = QGroupBox("Manual Channel Pressure (mbar)")
        pressure_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        pressure_layout = QVBoxLayout()

        pressure_row = QHBoxLayout()
        pressure_row.addWidget(QLabel("C1:"))
        p1_spin = QDoubleSpinBox()
        p1_spin.setRange(0.0, 2000.0)
        p1_spin.setDecimals(1)
        p1_spin.setSingleStep(5.0)
        pressure_row.addWidget(p1_spin)

        pressure_row.addWidget(QLabel("C2:"))
        p2_spin = QDoubleSpinBox()
        p2_spin.setRange(0.0, 2000.0)
        p2_spin.setDecimals(1)
        p2_spin.setSingleStep(5.0)
        pressure_row.addWidget(p2_spin)

        pressure_row.addWidget(QLabel("C3:"))
        p3_spin = QDoubleSpinBox()
        p3_spin.setRange(0.0, 2000.0)
        p3_spin.setDecimals(1)
        p3_spin.setSingleStep(5.0)
        pressure_row.addWidget(p3_spin)

        pressure_row.addWidget(QLabel("C4:"))
        p4_spin = QDoubleSpinBox()
        p4_spin.setRange(0.0, 2000.0)
        p4_spin.setDecimals(1)
        p4_spin.setSingleStep(5.0)
        pressure_row.addWidget(p4_spin)
        pressure_layout.addLayout(pressure_row)

        set_pressure_btn = QPushButton("Set Channel Pressures")
        set_pressure_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        set_pressure_btn.clicked.connect(lambda: self._set_manual_channel_pressures(p1_spin, p2_spin, p3_spin, p4_spin))
        pressure_layout.addWidget(set_pressure_btn)

        extra_pressure_row = QHBoxLayout()
        extra_pressure_row.addWidget(QLabel("Extra:"))
        extra_pressure_spin = QDoubleSpinBox()
        extra_pressure_spin.setRange(0.0, 2000.0)
        extra_pressure_spin.setDecimals(1)
        extra_pressure_spin.setSingleStep(5.0)
        extra_pressure_row.addWidget(extra_pressure_spin)
        extra_pressure_row.addStretch()
        pressure_layout.addLayout(extra_pressure_row)

        set_extra_pressure_btn = QPushButton("Set Extra Pressure")
        set_extra_pressure_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        set_extra_pressure_btn.clicked.connect(lambda: self._set_manual_extra_pressure(extra_pressure_spin))
        pressure_layout.addWidget(set_extra_pressure_btn)

        pressure_group.setLayout(pressure_layout)
        right_col.addWidget(pressure_group)

        # Sensor diagnostic section
        sensor_group = QGroupBox("Sensor Diagnostic")
        sensor_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        sensor_layout = QVBoxLayout()
        sensor_desc = QLabel("Read all sensors and print raw/corrected flow rates to terminal.")
        sensor_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        sensor_layout.addWidget(sensor_desc)
        sensor_btn = QPushButton("Read All Sensors")
        sensor_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        sensor_btn.clicked.connect(self._admin_read_all_sensors)
        sensor_layout.addWidget(sensor_btn)
        monitor_btn = QPushButton("Monitor Flow Rates")
        monitor_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        monitor_btn.clicked.connect(self._open_flow_rate_monitor)
        sensor_layout.addWidget(monitor_btn)
        sensor_group.setLayout(sensor_layout)
        right_col.addWidget(sensor_group)

        # Manual Load Line Section
        load_group = QGroupBox("Manual Load Line")
        load_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        load_layout = QVBoxLayout()

        load_row = QHBoxLayout()
        load_row.addWidget(QLabel("Line:"))
        load_line_combo = QComboBox()
        load_line_combo.addItems([str(i) for i in range(1, 4)])
        load_row.addWidget(load_line_combo)
        load_row.addWidget(QLabel("Plate:"))
        load_plate_combo = QComboBox()
        load_plate_combo.addItems([str(i) for i in range(1, 4)])
        load_row.addWidget(load_plate_combo)
        load_row.addWidget(QLabel("Row:"))
        load_row_combo = QComboBox()
        load_row_combo.addItems([chr(65 + i) for i in range(5)])
        load_row.addWidget(load_row_combo)
        load_row.addWidget(QLabel("Col:"))
        load_col_spin = QSpinBox()
        load_col_spin.setRange(1, 3)
        load_col_spin.setValue(1)
        load_row.addWidget(load_col_spin)
        load_row.addStretch()
        load_layout.addLayout(load_row)

        load_btn = QPushButton("Load Line")
        load_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")

        def _load_line():
            line = int(load_line_combo.currentText())
            plate = int(load_plate_combo.currentText())
            row = load_row_combo.currentIndex() + 1
            col = int(load_col_spin.value())

            QMessageBox.information(
                self,
                "Load Requested",
                f"Requesting load of line {line} from P{plate} {chr(64 + row)}{col}."
            )
            self.status_label.setText(f"Status: Loading - Line {line} from P{plate} {chr(64 + row)}{col}")

            def _do_load():
                try:
                    ok, err = self.control_api.admin_load_line_from_intake(line, plate, row, col)
                except Exception as e:
                    ok = False
                    err = str(e)
                if not ok:
                    QTimer.singleShot(
                        0,
                        lambda: QMessageBox.warning(self, "Load Failed", err or "Failed to load line.")
                    )
                    return
                QTimer.singleShot(
                    0,
                    lambda: QMessageBox.information(
                        self, "Load Queued", f"Loading line {line} from P{plate} {chr(64 + row)}{col}."
                    )
                )

            threading.Thread(target=_do_load, daemon=True).start()

        load_btn.clicked.connect(_load_line)
        load_layout.addWidget(load_btn)
        load_group.setLayout(load_layout)
        right_col.addWidget(load_group)

        # Manual declare loaded line section
        declare_group = QGroupBox("Manual Declare Loaded Line")
        declare_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        declare_layout = QVBoxLayout()
        declare_desc = QLabel("Mark a line as loaded in backend state (no robot movement).")
        declare_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        declare_layout.addWidget(declare_desc)

        declare_row = QHBoxLayout()
        declare_row.addWidget(QLabel("Line:"))
        declare_line_combo = QComboBox()
        declare_line_combo.addItems([str(i) for i in range(1, 4)])
        declare_row.addWidget(declare_line_combo)
        declare_row.addWidget(QLabel("Lipid:"))
        declare_lipid_combo = QComboBox()
        declare_lipid_combo.addItems(self.control_api.get_lipid_configs())
        declare_row.addWidget(declare_lipid_combo, 1)
        declare_layout.addLayout(declare_row)

        declare_vol_row = QHBoxLayout()
        declare_vol_row.addWidget(QLabel("Volume (uL):"))
        declare_vol_spin = QDoubleSpinBox()
        declare_vol_spin.setRange(0.0, 2000.0)
        declare_vol_spin.setDecimals(1)
        declare_vol_spin.setSingleStep(10.0)
        declare_vol_spin.setValue(450.0)
        declare_vol_row.addWidget(declare_vol_spin)
        declare_vol_row.addStretch()
        declare_layout.addLayout(declare_vol_row)

        declare_btn = QPushButton("Declare Line Loaded")
        declare_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        def _declare_loaded():
            line = int(declare_line_combo.currentText())
            lipid_name = str(declare_lipid_combo.currentText()).strip()
            vol = float(declare_vol_spin.value())
            ok, err = self.control_api.admin_declare_line_loaded(line, lipid_name, vol)
            if not ok:
                QMessageBox.warning(self, "Declare Failed", err or "Could not declare line loaded.")
                return
            self._sync_intake_visualization(force=True)
            self._refresh_queue_table()
            QMessageBox.information(self, "Declared", f"Line {line} set as loaded with {lipid_name} ({vol:.1f} uL).")
        declare_btn.clicked.connect(_declare_loaded)
        declare_layout.addWidget(declare_btn)
        declare_group.setLayout(declare_layout)
        right_col.addWidget(declare_group)

        # Manual declare empty/clean line section
        clear_group = QGroupBox("Manual Declare Empty/Clean Line")
        clear_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        clear_layout = QVBoxLayout()
        clear_desc = QLabel("Mark a line as empty/clean in backend state (no robot movement).")
        clear_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        clear_layout.addWidget(clear_desc)

        clear_row = QHBoxLayout()
        clear_row.addWidget(QLabel("Line:"))
        clear_line_combo = QComboBox()
        clear_line_combo.addItems([str(i) for i in range(1, 4)])
        clear_row.addWidget(clear_line_combo)
        clear_row.addStretch()
        clear_layout.addLayout(clear_row)

        clear_btn = QPushButton("Declare Line Empty/Clean")
        clear_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")

        def _declare_empty_clean():
            line = int(clear_line_combo.currentText())
            ok, err = self.control_api.admin_declare_line_empty_clean(line)
            if not ok:
                QMessageBox.warning(self, "Declare Failed", err or "Could not declare line empty/clean.")
                return
            self._sync_intake_visualization(force=True)
            self._refresh_queue_table()
            QMessageBox.information(self, "Declared", f"Line {line} set as empty/clean.")

        clear_btn.clicked.connect(_declare_empty_clean)
        clear_layout.addWidget(clear_btn)
        clear_group.setLayout(clear_layout)
        right_col.addWidget(clear_group)

        # Prime Lines Section
        prime_group = QGroupBox("Prime Lines")
        prime_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        prime_layout = QVBoxLayout()

        # Line selection checkboxes
        prime_desc = QLabel("Select which lines to prime:")
        prime_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        prime_layout.addWidget(prime_desc)

        line_checkboxes = {}
        checkbox_layout = QHBoxLayout()
        for line_num in [1, 2, 3]:
            cb = QCheckBox(f"Line {line_num}")
            cb.setChecked(True)  # All checked by default
            line_checkboxes[line_num] = cb
            checkbox_layout.addWidget(cb)
        prime_layout.addLayout(checkbox_layout)

        # Prime selected lines button
        prime_btn = QPushButton("Prime Selected Lines")
        prime_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        prime_btn.clicked.connect(lambda: self._prime_selected_lines(line_checkboxes))
        prime_layout.addWidget(prime_btn)

        prime_group.setLayout(prime_layout)
        right_col.addWidget(prime_group)

        left_col.addStretch()
        right_col.addStretch()

        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        dlg.exec()

    def _open_multiplate_calibration(self):
        """Calibrate first-well positions for output plates 1-6."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Multi-Plate Calibration")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        ok, err = self.control_api.enter_calibration_mode()
        if not ok:
            QMessageBox.warning(self, "Not Connected", err or "Microcontroller is not connected.")

        plate_row = QHBoxLayout()
        plate_row.addWidget(QLabel("Plate:"))
        plate_combo = QComboBox()
        plate_combo.addItems([str(i) for i in range(1, 7)])
        plate_row.addWidget(plate_combo)
        plate_row.addStretch()
        layout.addLayout(plate_row)

        saved_label = QLabel("Saved: none")
        saved_label.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        layout.addWidget(saved_label)

        tracked = {"H": 0, "V": 0}
        pos_label = QLabel("Tracked position (steps): H=0, V=0")
        layout.addWidget(pos_label)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step size (steps):"))
        step_spin = QSpinBox()
        step_spin.setRange(1, 10000)
        step_spin.setValue(50)
        step_row.addWidget(step_spin)
        step_row.addStretch()
        layout.addLayout(step_row)

        def _update_tracked_label():
            pos_label.setText(f"Tracked position (steps): H={tracked['H']}, V={tracked['V']}")

        def _load_saved():
            plate = int(plate_combo.currentText())
            calib = self.control_api.get_plate_calibration()
            entry = calib.get(str(plate)) or calib.get(plate)
            if entry and "stepsH" in entry and "stepsV" in entry:
                saved_label.setText(f"Saved: H={entry['stepsH']}, V={entry['stepsV']}")
            else:
                saved_label.setText("Saved: none")

        def _require_microcontroller():
            if not self.control_api.is_microcontroller_connected():
                QMessageBox.warning(self, "Not Connected", "Microcontroller is not connected.")
                return False
            return True

        def _home_to_plate():
            if not _require_microcontroller():
                return
            plate = int(plate_combo.currentText())
            try:
                self.control_api.home_stage_to_plate(plate)
            except Exception as e:
                QMessageBox.warning(self, "Home Failed", str(e))
                return
            calib = self.control_api.get_plate_calibration()
            entry = calib.get(str(plate)) or calib.get(plate)
            if entry and "stepsH" in entry and "stepsV" in entry:
                tracked["H"] = int(entry["stepsH"])
                tracked["V"] = int(entry["stepsV"])
            else:
                tracked["H"] = 0
                tracked["V"] = 0
            _update_tracked_label()
            _load_saved()

        def _jog(dx: int, dy: int):
            if not _require_microcontroller():
                return
            step = int(step_spin.value())
            steps_h = abs(dx) * step
            steps_v = abs(dy) * step
            if steps_h == 0 and steps_v == 0:
                return
            dir_h = "Away" if dx > 0 else "Towards"
            dir_v = "Away" if dy > 0 else "Towards"
            if steps_h == 0:
                dir_h = "Away"
            if steps_v == 0:
                dir_v = "Away"
            try:
                self.control_api.jog_stage(dir_h, dir_v, steps_h, steps_v)
            except Exception as e:
                QMessageBox.warning(self, "Move Failed", str(e))
                return
            tracked["H"] += dx * step
            tracked["V"] += dy * step
            _update_tracked_label()

        def _confirm():
            plate = int(plate_combo.currentText())
            self.control_api.set_plate_calibration(plate, tracked["H"], tracked["V"])
            _load_saved()
            QMessageBox.information(self, "Saved", f"Plate {plate} calibration saved.")

        home_btn = QPushButton("Home to Plate Start")
        home_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        home_btn.clicked.connect(_home_to_plate)
        layout.addWidget(home_btn)

        arrow_grid = QGridLayout()
        up_btn = QPushButton("▲")
        down_btn = QPushButton("▼")
        left_btn = QPushButton("◀")
        right_btn = QPushButton("▶")
        up_btn.clicked.connect(lambda: _jog(0, -1))
        down_btn.clicked.connect(lambda: _jog(0, 1))
        left_btn.clicked.connect(lambda: _jog(-1, 0))
        right_btn.clicked.connect(lambda: _jog(1, 0))
        arrow_grid.addWidget(up_btn, 0, 1)
        arrow_grid.addWidget(left_btn, 1, 0)
        arrow_grid.addWidget(right_btn, 1, 2)
        arrow_grid.addWidget(down_btn, 2, 1)
        layout.addLayout(arrow_grid)

        confirm_btn = QPushButton("Confirm Calibration")
        confirm_btn.setStyleSheet("background-color: #7CFC90; font-size: 11px; padding: 8px;")
        confirm_btn.clicked.connect(_confirm)
        layout.addWidget(confirm_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        def _on_plate_changed():
            tracked["H"] = 0
            tracked["V"] = 0
            _update_tracked_label()
            _load_saved()

        plate_combo.currentIndexChanged.connect(_on_plate_changed)
        _load_saved()

        try:
            dlg.exec()
        finally:
            self.control_api.exit_calibration_mode()

    def _start_without_loading(self, dlg):
        """Start next experiment without loading lipids."""
        ok, err = self.control_api.start_without_loading()
        if not ok:
            QMessageBox.warning(self, "Cannot start", err or "Start conditions not met.")
            return
        self.stop_btn.setEnabled(True)
        dlg.accept()
        QMessageBox.information(self, "Started", "Experiment started without loading phase.")

    def _admin_test_line_switch_protocol(self):
        """Admin: select lines and run switch protocol (parallel clean, sequential load)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Run Line Switch Protocol")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Select lines to switch for the next pending experiment:"))

        checks = {}
        row = QHBoxLayout()
        for ln in (1, 2, 3):
            cb = QCheckBox(f"Line {ln}")
            cb.setChecked(True)
            checks[ln] = cb
            row.addWidget(cb)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        run_btn = QPushButton("Run")
        cancel_btn = QPushButton("Cancel")
        buttons.addWidget(run_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        def _run():
            selected = [ln for ln, cb in checks.items() if cb.isChecked()]
            ok, err = self.control_api.admin_prepare_line_switch(selected)
            if not ok:
                QMessageBox.warning(self, "Switch Protocol Failed", err or "Could not start switch protocol.")
                return
            QMessageBox.information(self, "Switch Protocol Started", err or "Switch protocol started.")
            dlg.accept()

        run_btn.clicked.connect(_run)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _admin_test_line_switch_protocol_legacy(self):
        """Legacy helper retained for compatibility."""
        ok, err = self.control_api.admin_test_line_switch_protocol()
        if not ok:
            QMessageBox.warning(self, "Switch Test Failed", err or "Could not start switch test.")
            return
        QMessageBox.information(self, "Switch Test Started", err or "Switch test started.")

    def _open_well_to_well_test_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Well-to-Well Movement")
        layout = QVBoxLayout(dlg)

        desc = QLabel(
            "Move through every well from the selected start to end position, inclusive.\n"
            "Traversal uses plate, row, column ordering and reverses automatically if end is earlier."
        )
        desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        layout.addWidget(desc)

        start_group = QGroupBox("Start Well")
        start_layout = QHBoxLayout()
        start_layout.addWidget(QLabel("Plate:"))
        start_plate = QComboBox()
        start_plate.addItems([str(i) for i in range(1, 7)])
        start_layout.addWidget(start_plate)
        start_layout.addWidget(QLabel("Row:"))
        start_row = QComboBox()
        start_row.addItems([chr(65 + i) for i in range(8)])
        start_layout.addWidget(start_row)
        start_layout.addWidget(QLabel("Col:"))
        start_col = QSpinBox()
        start_col.setRange(1, 12)
        start_col.setValue(1)
        start_layout.addWidget(start_col)
        start_group.setLayout(start_layout)
        layout.addWidget(start_group)

        end_group = QGroupBox("End Well")
        end_layout = QHBoxLayout()
        end_layout.addWidget(QLabel("Plate:"))
        end_plate = QComboBox()
        end_plate.addItems([str(i) for i in range(1, 7)])
        end_plate.setCurrentIndex(0)
        end_layout.addWidget(end_plate)
        end_layout.addWidget(QLabel("Row:"))
        end_row = QComboBox()
        end_row.addItems([chr(65 + i) for i in range(8)])
        end_layout.addWidget(end_row)
        end_layout.addWidget(QLabel("Col:"))
        end_col = QSpinBox()
        end_col.setRange(1, 12)
        end_col.setValue(12)
        end_layout.addWidget(end_col)
        end_group.setLayout(end_layout)
        layout.addWidget(end_group)

        pre_collect_row = QHBoxLayout()
        pre_collect_row.addWidget(QLabel("Wait before collection at each well (s):"))
        pre_collect_spin = QDoubleSpinBox()
        pre_collect_spin.setRange(0.0, 600.0)
        pre_collect_spin.setDecimals(1)
        pre_collect_spin.setSingleStep(0.5)
        pre_collect_spin.setValue(0.0)
        pre_collect_row.addWidget(pre_collect_spin)
        pre_collect_row.addStretch()
        layout.addLayout(pre_collect_row)

        hold_row = QHBoxLayout()
        hold_row.addWidget(QLabel("Hold time at each well (s):"))
        hold_spin = QDoubleSpinBox()
        hold_spin.setRange(0.0, 600.0)
        hold_spin.setDecimals(1)
        hold_spin.setSingleStep(0.5)
        hold_spin.setValue(0.0)
        hold_row.addWidget(hold_spin)
        hold_row.addStretch()
        layout.addLayout(hold_row)

        collect_chk = QCheckBox("Perform collection motion at each well")
        collect_chk.setChecked(False)
        layout.addWidget(collect_chk)

        collect_desc = QLabel(
            "Collection motion means: servo to collect, Z down, hold, then Z up and back to waste before moving on."
        )
        collect_desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        layout.addWidget(collect_desc)

        buttons = QHBoxLayout()
        run_btn = QPushButton("Run")
        cancel_btn = QPushButton("Cancel")
        buttons.addWidget(run_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        def _run():
            ok, err = self.control_api.admin_run_well_to_well_test(
                start_plate=int(start_plate.currentText()),
                start_row=start_row.currentIndex() + 1,
                start_col=int(start_col.value()),
                end_plate=int(end_plate.currentText()),
                end_row=end_row.currentIndex() + 1,
                end_col=int(end_col.value()),
                pre_collect_wait_s=float(pre_collect_spin.value()),
                hold_time_s=float(hold_spin.value()),
                perform_collection=bool(collect_chk.isChecked()),
            )
            if not ok:
                QMessageBox.warning(self, "Well Sweep Failed", err or "Could not start well sweep.")
                return
            QMessageBox.information(self, "Well Sweep Started", err or "Well sweep started.")
            dlg.accept()

        run_btn.clicked.connect(_run)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _open_remove_stopper_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Remove Stoppers")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        desc = QLabel(
            "Select an intake well and run stopper removal.\n"
            "Sequence: pick from selected well -> drop near P3 A3 (+forward 60) -> return home."
        )
        desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        layout.addWidget(desc)

        row = QHBoxLayout()
        row.addWidget(QLabel("Plate:"))
        plate_combo = QComboBox()
        plate_combo.addItems([str(i) for i in range(1, 4)])
        row.addWidget(plate_combo)
        row.addWidget(QLabel("Row:"))
        row_combo = QComboBox()
        row_combo.addItems([chr(65 + i) for i in range(5)])
        row.addWidget(row_combo)
        row.addWidget(QLabel("Col:"))
        col_spin = QSpinBox()
        col_spin.setRange(1, 3)
        col_spin.setValue(1)
        row.addWidget(col_spin)
        row.addStretch()
        layout.addLayout(row)

        btn_row = QHBoxLayout()
        remove_btn = QPushButton("Remove")
        remove_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        close_btn = QPushButton("Close")
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        def _run_remove():
            plate = int(plate_combo.currentText())
            row_idx = row_combo.currentIndex() + 1
            col = int(col_spin.value())

            remove_btn.setEnabled(False)
            result = {"done": False, "ok": False, "err": ""}

            def _worker():
                try:
                    ok, err = self.control_api.admin_remove_stopper(plate, row_idx, col)
                except Exception as e:
                    ok = False
                    err = str(e)
                result["ok"] = bool(ok)
                result["err"] = str(err or "")
                result["done"] = True

            threading.Thread(target=_worker, daemon=True).start()

            poll_timer = QTimer(dlg)
            def _check_done():
                if not result["done"]:
                    return
                poll_timer.stop()
                remove_btn.setEnabled(True)
                if not result["ok"]:
                    QMessageBox.warning(self, "Remove Stoppers Failed", result["err"] or "Stopper removal failed.")
                    return
                QMessageBox.information(self, "Remove Stoppers", "Stopper removal sequence completed.")
            poll_timer.timeout.connect(_check_done)
            poll_timer.start(100)

        remove_btn.clicked.connect(_run_remove)
        close_btn.clicked.connect(dlg.accept)
        dlg.exec()

    def _open_dobot_control_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Dobot Control")
        dlg.setMinimumWidth(540)
        layout = QVBoxLayout(dlg)

        info = QLabel(
            "Manual Dobot controls. For jog buttons, first run 'Move Above Intake Well'\n"
            "to initialize the jog reference pose."
        )
        info.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        layout.addWidget(info)

        hover_group = QGroupBox("Move to Intake Well (Hover Only)")
        hover_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        hover_layout = QVBoxLayout()
        hover_row = QHBoxLayout()
        hover_row.addWidget(QLabel("Plate:"))
        hover_plate = QComboBox()
        hover_plate.addItems([str(i) for i in range(1, 4)])
        hover_row.addWidget(hover_plate)
        hover_row.addWidget(QLabel("Row:"))
        hover_row_combo = QComboBox()
        hover_row_combo.addItems([chr(65 + i) for i in range(5)])
        hover_row.addWidget(hover_row_combo)
        hover_row.addWidget(QLabel("Col:"))
        hover_col = QSpinBox()
        hover_col.setRange(1, 3)
        hover_col.setValue(1)
        hover_row.addWidget(hover_col)
        hover_row.addStretch()
        hover_layout.addLayout(hover_row)
        hover_btn = QPushButton("Move Above Intake Well")
        hover_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        hover_layout.addWidget(hover_btn)
        hover_group.setLayout(hover_layout)
        layout.addWidget(hover_group)

        gripper_group = QGroupBox("Gripper")
        gripper_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        gripper_layout = QHBoxLayout()
        grip_on_btn = QPushButton("Gripper ON")
        grip_off_btn = QPushButton("Gripper OFF")
        grip_on_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        grip_off_btn.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        gripper_layout.addWidget(grip_on_btn)
        gripper_layout.addWidget(grip_off_btn)
        gripper_group.setLayout(gripper_layout)
        layout.addWidget(gripper_group)

        jog_group = QGroupBox("Manual Jog")
        jog_group.setStyleSheet("QGroupBox { font-weight: bold; color: #FFA500; }")
        jog_layout = QVBoxLayout()
        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Linear step (mm):"))
        linear_step = QDoubleSpinBox()
        linear_step.setRange(0.1, 50.0)
        linear_step.setDecimals(1)
        linear_step.setSingleStep(0.5)
        linear_step.setValue(2.0)
        step_row.addWidget(linear_step)
        step_row.addWidget(QLabel("Rotate step (deg):"))
        rotate_step = QDoubleSpinBox()
        rotate_step.setRange(0.1, 45.0)
        rotate_step.setDecimals(1)
        rotate_step.setSingleStep(0.5)
        rotate_step.setValue(2.0)
        step_row.addWidget(rotate_step)
        step_row.addStretch()
        jog_layout.addLayout(step_row)

        jog_grid = QGridLayout()
        up_btn = QPushButton("Up")
        down_btn = QPushButton("Down")
        left_btn = QPushButton("Left")
        right_btn = QPushButton("Right")
        fwd_btn = QPushButton("Forward")
        back_btn = QPushButton("Back")
        cw_btn = QPushButton("Rotate CW")
        ccw_btn = QPushButton("Rotate CCW")
        for b in (up_btn, down_btn, left_btn, right_btn, fwd_btn, back_btn, cw_btn, ccw_btn):
            b.setStyleSheet("background-color: #4ECDC4; font-size: 11px; padding: 8px;")
        jog_grid.addWidget(up_btn, 0, 1)
        jog_grid.addWidget(left_btn, 1, 0)
        jog_grid.addWidget(fwd_btn, 1, 1)
        jog_grid.addWidget(right_btn, 1, 2)
        jog_grid.addWidget(down_btn, 2, 1)
        jog_grid.addWidget(back_btn, 3, 1)
        jog_grid.addWidget(ccw_btn, 4, 0)
        jog_grid.addWidget(cw_btn, 4, 2)
        jog_layout.addLayout(jog_grid)
        jog_group.setLayout(jog_layout)
        layout.addWidget(jog_group)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        def _run_async(call_fn, fail_title, fail_default):
            def _worker():
                try:
                    ok, err = call_fn()
                except Exception as e:
                    ok = False
                    err = str(e)
                if not ok:
                    QTimer.singleShot(0, lambda: QMessageBox.warning(self, fail_title, err or fail_default))
            threading.Thread(target=_worker, daemon=True).start()

        def _move_hover():
            plate = int(hover_plate.currentText())
            row = hover_row_combo.currentIndex() + 1
            col = int(hover_col.value())
            _run_async(
                lambda: self.control_api.admin_move_dobot_to_intake_hover(plate, row, col),
                "Dobot Move Failed",
                "Failed to move Dobot above intake well.",
            )

        def _set_gripper(state_on: bool):
            _run_async(
                lambda: self.control_api.admin_set_dobot_gripper(state_on),
                "Gripper Command Failed",
                "Failed to set gripper state.",
            )

        def _jog(dx: float = 0.0, dy: float = 0.0, dz: float = 0.0, dr: float = 0.0):
            _run_async(
                lambda: self.control_api.admin_jog_dobot(dx=dx, dy=dy, dz=dz, dr=dr),
                "Manual Jog Failed",
                "Failed to jog Dobot.",
            )

        hover_btn.clicked.connect(_move_hover)
        grip_on_btn.clicked.connect(lambda: _set_gripper(True))
        grip_off_btn.clicked.connect(lambda: _set_gripper(False))
        up_btn.clicked.connect(lambda: _jog(dz=float(linear_step.value())))
        down_btn.clicked.connect(lambda: _jog(dz=-float(linear_step.value())))
        left_btn.clicked.connect(lambda: _jog(dx=-float(linear_step.value())))
        right_btn.clicked.connect(lambda: _jog(dx=float(linear_step.value())))
        fwd_btn.clicked.connect(lambda: _jog(dy=float(linear_step.value())))
        back_btn.clicked.connect(lambda: _jog(dy=-float(linear_step.value())))
        cw_btn.clicked.connect(lambda: _jog(dr=float(rotate_step.value())))
        ccw_btn.clicked.connect(lambda: _jog(dr=-float(rotate_step.value())))
        dlg.exec()

    def _open_random_dobot_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Move Random Dobot")
        layout = QVBoxLayout(dlg)

        desc = QLabel(
            "Continuously pick a random input nozzle (1-3) from holding, place it into a random\n"
            "inlet-plate well (plates 1-3, rows A-E, cols 1-3), then return it to the same holding position.\n"
            "The routine repeats until Stop is pressed."
        )
        desc.setStyleSheet("color: #AAAAAA; font-size: 10px;")
        layout.addWidget(desc)

        state_label = QLabel("State: Idle")
        layout.addWidget(state_label)

        buttons = QHBoxLayout()
        run_btn = QPushButton("Run")
        stop_btn = QPushButton("Stop")
        close_btn = QPushButton("Close")
        buttons.addWidget(run_btn)
        buttons.addWidget(stop_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        def _sync_buttons():
            running = bool(self.control_api.admin_is_random_dobot_running())
            run_btn.setEnabled(not running)
            stop_btn.setEnabled(running)
            state_label.setText("State: Running" if running else "State: Idle")

        def _run():
            ok, err = self.control_api.admin_start_random_dobot()
            if not ok:
                QMessageBox.warning(self, "Random Dobot Failed", err or "Could not start random Dobot movement.")
                return
            _sync_buttons()

        def _stop():
            ok, err = self.control_api.admin_stop_random_dobot()
            if not ok:
                QMessageBox.warning(self, "Random Dobot Stop Failed", err or "Could not stop random Dobot movement.")
                return
            state_label.setText("State: Stopping...")

        def _close():
            if self.control_api.admin_is_random_dobot_running():
                self.control_api.admin_stop_random_dobot()
            dlg.accept()

        timer = QTimer(dlg)
        timer.timeout.connect(_sync_buttons)
        timer.start(300)
        _sync_buttons()

        run_btn.clicked.connect(_run)
        stop_btn.clicked.connect(_stop)
        close_btn.clicked.connect(_close)
        dlg.finished.connect(lambda _: self.control_api.admin_stop_random_dobot() if self.control_api.admin_is_random_dobot_running() else None)
        dlg.exec()

    def _set_manual_servo_position(self, servo_combo, angle_combo):
        """Manually set a rotary servo position."""
        try:
            servo_number = int(servo_combo.currentText())
            angle_index = angle_combo.currentIndex()
            
            # Map combo index to angle
            angle_map = {0: 40, 1: 80, 2: 125}
            angle = angle_map[angle_index]
            
            ok, err = self.control_api.admin_set_servo_position(servo_number, angle)
            if not ok:
                QMessageBox.warning(self, "Not Connected", err or "Microcontroller is not connected.")
                return
            
            QMessageBox.information(
                self, 
                "Servo Set", 
                f"Servo {servo_number} set to {angle}°"
            )
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to set servo position: {str(e)}")

    def _set_manual_dobot_valve(self, line_combo, state_combo):
        """Manually set a Dobot valve on/off."""
        try:
            line = int(line_combo.currentText())
            state = "on" if state_combo.currentIndex() == 0 else "off"
            ok, err = self.control_api.admin_set_dobot_valve(line, state)
            if not ok:
                QMessageBox.warning(self, "Not Connected", err or "Dobot is not connected.")
                return
            QMessageBox.information(self, "Valve Set", f"Dobot valve line {line} set to {state.upper()}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to set Dobot valve: {str(e)}")

    def _set_manual_channel_pressures(self, p1_spin, p2_spin, p3_spin, p4_spin):
        """Manually set channel pressures for C1..C4."""
        try:
            p1 = float(p1_spin.value())
            p2 = float(p2_spin.value())
            p3 = float(p3_spin.value())
            p4 = float(p4_spin.value())
            ok, err = self.control_api.admin_set_channel_pressures(p1, p2, p3, p4)
            if not ok:
                QMessageBox.warning(self, "Set Pressure Failed", err or "Could not set pressures.")
                return
            QMessageBox.information(
                self,
                "Pressures Set",
                f"C1={p1:.1f} mbar, C2={p2:.1f} mbar, C3={p3:.1f} mbar, C4={p4:.1f} mbar",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to set pressures: {str(e)}")

    def _set_manual_extra_pressure(self, p_spin):
        """Manually set the extra pressure controller pressure."""
        try:
            pressure = float(p_spin.value())
            ok, err = self.control_api.admin_set_extra_pressure(pressure)
            if not ok:
                QMessageBox.warning(self, "Set Extra Pressure Failed", err or "Could not set extra pressure.")
                return
            QMessageBox.information(self, "Extra Pressure Set", f"Extra={pressure:.1f} mbar")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to set extra pressure: {str(e)}")

    def _admin_read_all_sensors(self):
        """Admin: print raw/corrected flow rates for all channels."""
        try:
            print("[GUI] Admin sensor diagnostic requested")
            ok, err = self.control_api.admin_read_all_sensors()
            if not ok:
                QMessageBox.warning(self, "Sensor Read Failed", err or "Could not read sensors.")
                return
                QMessageBox.information(self, "Sensor Read", err or "Sensor diagnostic printed to terminal.")
        except Exception as e:
            QMessageBox.critical(self, "Sensor Read Failed", str(e))

    def _open_flow_rate_monitor(self):
        """Admin: live monitor of raw and corrected sensor flow rates."""
        try:
            status = self.control_api.get_status()
            if status.get("current_experiment") and status.get("microfluidic_state") not in ("Idle", "Ready", "Stopped"):
                QMessageBox.warning(self, "Unavailable", "Cannot start monitor while an experiment is running.")
                return

            dlg = QDialog(self)
            dlg.setWindowTitle("Monitor Flow Rates")
            dlg.setMinimumSize(900, 620)
            layout = QVBoxLayout(dlg)

            info_label = QLabel("Updating every 0.1s")
            info_label.setStyleSheet("color: #AAAAAA; font-size: 10px;")
            layout.addWidget(info_label)

            raw_plot = pg.PlotWidget(title="Raw Flow Sensor Reads")
            raw_plot.setLabel("left", "Raw")
            raw_plot.setLabel("bottom", "Time (s)")
            raw_plot.showGrid(x=True, y=True, alpha=0.3)
            layout.addWidget(raw_plot)

            corr_plot = pg.PlotWidget(title="Corrected Flow Sensor Reads (uL/min)")
            corr_plot.setLabel("left", "uL/min")
            corr_plot.setLabel("bottom", "Time (s)")
            corr_plot.showGrid(x=True, y=True, alpha=0.3)
            layout.addWidget(corr_plot)

            values_label = QLabel("Current: waiting for first sample...")
            values_label.setStyleSheet("color: #000000; font-size: 14px; font-weight: 600;")
            layout.addWidget(values_label)

            close_btn = QPushButton("Close")
            close_btn.clicked.connect(dlg.accept)
            layout.addWidget(close_btn)

            ch_colors = ["#FF6B6B", "#4ECDC4", "#FFD93D", "#9B59B6"]
            raw_curves = [raw_plot.plot(pen=pg.mkPen(c, width=2), name=f"Ch{i+1}") for i, c in enumerate(ch_colors)]
            corr_curves = [corr_plot.plot(pen=pg.mkPen(c, width=2), name=f"Ch{i+1}") for i, c in enumerate(ch_colors)]

            time_data = []
            raw_data = [[], [], [], []]
            corr_data = [[], [], [], []]
            max_points = 1200
            t0 = time.time()

            timer = QTimer(dlg)

            def _tick():
                ok, payload = self.control_api.admin_get_sensor_snapshot()
                if not ok:
                    values_label.setText(f"Current: sensor read failed ({payload})")
                    return

                now_s = time.time() - t0
                rows = payload
                time_data.append(now_s)
                for idx in range(4):
                    raw_v = rows[idx]["raw"] if idx < len(rows) else 0.0
                    corr_v = rows[idx]["corrected"] if idx < len(rows) else None
                    raw_data[idx].append(float(raw_v))
                    corr_data[idx].append(float(corr_v) if corr_v is not None else np.nan)

                if len(time_data) > max_points:
                    time_data.pop(0)
                    for idx in range(4):
                        raw_data[idx].pop(0)
                        corr_data[idx].pop(0)

                for idx in range(4):
                    raw_curves[idx].setData(time_data, raw_data[idx])
                    corr_curves[idx].setData(time_data, corr_data[idx])

                values_label.setText(
                    "Current: "
                    + " | ".join(
                        f"Ch{r['channel']} raw={r['raw']:.4f}, corr={('NA' if r['corrected'] is None else ('%.4f' % r['corrected']))}"
                        for r in rows
                    )
                )

            timer.timeout.connect(_tick)
            timer.start(100)
            dlg.finished.connect(lambda _: timer.stop())
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Monitor Failed", str(e))

    def _prime_all_lines_manual(self):
        """Manually prime all lines simultaneously."""
        try:
            loaded_lines = [1, 2, 3]  # Prime all lines
            print(f"[GUI] Priming all lines: {loaded_lines}")
            ok, err = self.control_api.admin_prime_lines(loaded_lines)
            if not ok:
                QMessageBox.warning(self, "Not Connected", err or "Microcontroller is not connected.")
                return
            QMessageBox.information(self, "Priming started", "Priming all lines (1, 2, 3)")
        except Exception as e:
            print(f"[GUI] Prime error: {e}")
            QMessageBox.critical(self, "Error", f"Failed to start priming: {str(e)}")

    def _prime_selected_lines(self, line_checkboxes: dict):
        """Prime only the selected lines based on checkbox state."""
        try:
            loaded_lines = [line_num for line_num, checkbox in line_checkboxes.items() if checkbox.isChecked()]
            
            if not loaded_lines:
                QMessageBox.warning(self, "No lines selected", "Please select at least one line to prime.")
                return
            
            print(f"[GUI] Priming selected lines: {loaded_lines}")
            ok, err = self.control_api.admin_prime_lines(loaded_lines)
            if not ok:
                QMessageBox.warning(self, "Not Connected", err or "Microcontroller is not connected.")
                return
            lines_str = ", ".join(str(l) for l in loaded_lines)
            QMessageBox.information(self, "Priming started", f"Priming lines: {lines_str}")
        except Exception as e:
            print(f"[GUI] Prime error: {e}")
            QMessageBox.critical(self, "Error", f"Failed to start priming: {str(e)}")

    def _organise_queue(self):
        # Placeholder for reordering UI
        pass

    def _update_start_enabled(self):
        all_connected = all(self._conn_state.values())
        self.start_btn.setEnabled(all_connected)
        self.clean_btn.setEnabled(all_connected)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
            child = item.layout()
            if child:
                self._clear_layout(child)

    def _restore_intake_visualization(self):
        """Restore intake well visualization from saved allocations."""
        self._sync_intake_visualization(force=True)

    def _sync_intake_visualization(self, force: bool = False):
        """Sync intake well widgets to backend allocations (clears loaded wells)."""
        allocations = self.control_api.get_lipid_allocations() or {}
        sig = tuple(
            sorted(
                (int(k[0]), int(k[1]), int(k[2]), str(v))
                for k, v in allocations.items()
            )
        )
        if (not force) and sig == self._last_intake_alloc_sig:
            return
        self._last_intake_alloc_sig = sig

        for well in self.intake_wells.values():
            well.set_lipid(None, "#555555")

        for (plate, row, col), lipid_name in allocations.items():
            pos = (int(plate), int(row), int(col))
            if pos not in self.intake_wells:
                continue
            cfg = self.control_api.load_lipid_config(lipid_name) or {}
            color_hex = cfg.get("color", "#555555")
            self.intake_wells[pos].set_lipid(lipid_name, color_hex)

    def _import_experiments_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Experiments CSV", "", "CSV Files (*.csv)")
        if not path:
            return
        ok, msg = self.control_api.import_experiments_from_csv(path)
        if not ok:
            QMessageBox.warning(self, "Import Failed", msg or "Could not import experiments.")
            return
        self._refresh_queue_table()
        self._sync_plate_visualization()
        QMessageBox.information(self, "Import Complete", msg)

    def _move_selected_experiment(self, direction: int):
        """Move selected experiment up (-1) or down (+1) in queue."""
        item = self.exp_table.currentItem()
        if not item:
            return
        exp_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not exp_id:
            return
        ok = self.control_api.move_experiment_up(exp_id) if direction < 0 else self.control_api.move_experiment_down(exp_id)
        if ok:
            self._refresh_queue_table()
            self._update_experiment_queue_buttons()

    def _delete_selected_experiment(self):
        """Delete the selected experiment from the queue."""
        item = self.exp_table.currentItem()
        if not item:
            return
        exp_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not exp_id:
            return
        reply = QMessageBox.warning(
            self,
            "Delete Experiment",
            f"Delete selected experiment?\n\nExp ID: {exp_id}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.control_api.delete_experiment(exp_id)
        self._refresh_queue_table()
        self._sync_plate_visualization()
        self._update_experiment_queue_buttons()

    def _update_experiment_queue_buttons(self):
        has_selection = bool(getattr(self, "exp_table", None) and self.exp_table.currentItem())
        for btn_name in ("move_up_btn", "move_down_btn", "delete_exp_btn"):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                btn.setEnabled(has_selection)
        self._selected_start_exp_id = None
        mode_lbl = getattr(self, "start_well_mode_label", None)
        item = self.exp_table.currentItem() if getattr(self, "exp_table", None) else None
        if item:
            exp_id = item.data(0, Qt.ItemDataRole.UserRole)
            status_txt = (item.text(6) or "").strip().lower()
            if exp_id and status_txt == "pending":
                self._selected_start_exp_id = str(exp_id)
                exp = self.control_api.get_experiment(exp_id)
                exp_name = getattr(exp, "name", str(exp_id)) if exp else str(exp_id)
                if mode_lbl is not None:
                    mode_lbl.setText(f"Plate Click Mode: Assign start for '{exp_name}'")
                return
        if mode_lbl is not None:
            mode_lbl.setText("Plate Click Mode: Global (no pending experiment selected)")




