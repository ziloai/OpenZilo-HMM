# SPDX-License-Identifier: MPL-2.0
"""Design tokens from DESIGN.md, adapted for a dense tool. Change colors only here."""
from __future__ import annotations

GALLERY_WHITE = "#ffffff"
STUDIO_MIST = "#f5f5f7"
PAPER_FROST = "#fafafc"
HAIRLINE = "#d6d6d6"
CONTROL_GRAY = "#e6e6e8"
INK = "#1d1d1f"
SLATE = "#707070"
STEEL = "#86868b"
APPLE_BLUE = "#0066cc"
PRICING_BLUE = "#0071e3"
LAUNCH_ORANGE = "#b64400"

# Additions the marketing spec lacks. Series colors passed the CVD validator on
# white, in this fixed X/Y/Z order. Status colors always accompany a text label.
AXIS_COLORS = ("#0066cc", "#c2410c", "#0f8a5f")
GOOD = "#248a3d"
WARNING = LAUNCH_ORANGE
CRITICAL = "#d70015"
REGION = "#8944ab"
SCRIM = INK  # Dims the window behind a dialog, at SCRIM_OPACITY.
SCRIM_OPACITY = 0.28

FONT_FAMILIES = ("SF Pro Text", "PingFang SC", "Inter", "Helvetica Neue", "Segoe UI",
                 "Microsoft YaHei UI", "Noto Sans CJK SC", "Arial")

CONTENT_MAX_WIDTH = 1240
PAGE_MARGIN = 40
SECTION_GAP = 40
CARD_PADDING = 28
ELEMENT_GAP = 12
CHART_HEIGHT = 500
CAPTURE_CHART_HEIGHT = 420  # Both panels fit above the fold at the default window size.
DIALOG_WIDTH = 440
DIALOG_BODY_MAX_HEIGHT = 320  # Longer messages scroll inside the dialog.
CONTRIBUTOR_TILE = 112  # QSS sizes beat setFixedSize, so this is set in the stylesheet.
MOTION_FAST = 150  # ms: toggles and small state changes.
MOTION = 220  # ms: navigation and panels.

STYLESHEET = f"""
QWidget#canvas, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {GALLERY_WHITE}; }}
QScrollArea {{ border: none; }}
QWidget#plain {{ background: {GALLERY_WHITE}; }}
QWidget#sidebar {{ background: {STUDIO_MIST}; border-right: 1px solid {CONTROL_GRAY}; }}
QFrame#card {{ background: {GALLERY_WHITE}; border: 1px solid {CONTROL_GRAY}; border-radius: 28px; }}
QFrame#navIndicator {{ background: {CONTROL_GRAY}; border: none; border-radius: 10px; }}
QFrame#hairline {{ background: {CONTROL_GRAY}; border: none; max-height: 1px; min-height: 1px; }}
QWidget#logBar {{ background: {PAPER_FROST}; border-top: 1px solid {CONTROL_GRAY}; }}

QLabel {{ color: {INK}; background: transparent; }}
QLabel[role="title"] {{ font-size: 40px; font-weight: 600; }}
QLabel[role="brand"] {{ font-size: 19px; font-weight: 600; }}
QLabel[role="section"] {{ font-size: 24px; font-weight: 600; }}
QLabel[role="cardTitle"] {{ font-size: 17px; font-weight: 600; }}
QLabel[role="strong"] {{ font-weight: 600; }}
QLabel[role="caption"] {{ font-size: 12px; color: {SLATE}; }}
QLabel[role="stat"] {{ font-size: 28px; font-weight: 600; }}
QLabel[role="result"] {{ font-size: 40px; font-weight: 600; }}
QLabel[tone="muted"] {{ color: {SLATE}; }}
QLabel[tone="good"] {{ color: {GOOD}; }}
QLabel[tone="warning"] {{ color: {WARNING}; }}
QLabel[tone="critical"] {{ color: {CRITICAL}; }}

QPushButton {{ font-size: 13px; padding: 0 16px; min-height: 30px; max-height: 30px;
  border-radius: 15px; color: {INK}; background: transparent; border: 1px solid {STEEL}; }}
QPushButton:hover {{ background: {STUDIO_MIST}; }}
QPushButton:disabled {{ color: {STEEL}; border-color: {CONTROL_GRAY}; background: transparent; }}
QPushButton[kind="primary"] {{ background: {PRICING_BLUE}; color: {GALLERY_WHITE}; border-color: {PRICING_BLUE}; }}
QPushButton[kind="primary"]:hover {{ background: {APPLE_BLUE}; border-color: {APPLE_BLUE}; }}
QPushButton[kind="primary"]:disabled {{ background: {CONTROL_GRAY}; color: {STEEL}; border-color: {CONTROL_GRAY}; }}
QPushButton[kind="record"] {{ background: {CRITICAL}; color: {GALLERY_WHITE}; border-color: {CRITICAL}; }}
QPushButton[kind="record"]:hover {{ background: #bf0013; border-color: #bf0013; }}
QPushButton[kind="record"]:disabled {{ background: {CONTROL_GRAY}; color: {STEEL}; border-color: {CONTROL_GRAY}; }}
QPushButton[kind="danger"] {{ color: {CRITICAL}; border-color: {CRITICAL}; }}
QPushButton[kind="danger"]:disabled {{ color: {STEEL}; border-color: {CONTROL_GRAY}; }}
QPushButton[kind="link"] {{ color: {APPLE_BLUE}; border: none; padding: 0; background: transparent; }}
QPushButton[kind="link"]:hover {{ text-decoration: underline; background: transparent; }}
QPushButton[kind="link"]:disabled {{ color: {STEEL}; }}
QPushButton[kind="nav"] {{ text-align: left; font-size: 14px; font-weight: 500; border: none;
  border-radius: 10px; padding: 0 12px; min-height: 36px; max-height: 36px; }}
QPushButton[kind="nav"]:hover {{ background: rgba(0, 0, 0, 0.04); }}
QPushButton[kind="nav"]:checked {{ background: transparent; font-weight: 600; }}
QPushButton#contributor {{ background: transparent; border: none; border-radius: 14px; padding: 0;
  min-width: {CONTRIBUTOR_TILE}px; max-width: {CONTRIBUTOR_TILE}px;
  min-height: {CONTRIBUTOR_TILE}px; max-height: {CONTRIBUTOR_TILE}px; }}
QPushButton#contributor:hover {{ background: {STUDIO_MIST}; }}
QPushButton#deviceCard {{ text-align: left; background: {GALLERY_WHITE}; border: none; border-radius: 20px;
  min-height: 0; max-height: 16777215; padding: 0; }}
QPushButton#deviceCard:hover {{ background: {PAPER_FROST}; }}
QPushButton#deviceCard[current="true"] {{ background: {CONTROL_GRAY}; }}

QLineEdit, QComboBox {{ font-size: 13px; background: {GALLERY_WHITE}; color: {INK}; border: 1px solid {STEEL};
  border-radius: 16px; min-height: 30px; max-height: 30px; padding: 0 14px;
  selection-background-color: {PRICING_BLUE}; selection-color: {GALLERY_WHITE}; }}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {PRICING_BLUE}; }}
QLineEdit:read-only {{ background: {STUDIO_MIST}; border-color: {CONTROL_GRAY}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {STEEL}; border-color: {CONTROL_GRAY}; background: {PAPER_FROST}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: none; }}
QComboBox QAbstractItemView {{ background: {GALLERY_WHITE}; color: {INK}; border: 1px solid {HAIRLINE};
  selection-background-color: {CONTROL_GRAY}; selection-color: {INK}; outline: none; }}

QPlainTextEdit {{ font-size: 12px; background: {STUDIO_MIST}; color: {INK}; border: none;
  border-radius: 10px; padding: 8px; }}
QListWidget#logList {{ background: {GALLERY_WHITE}; border: 1px solid {CONTROL_GRAY}; border-radius: 10px; outline: none; }}

QTableWidget {{ background: transparent; color: {INK}; border: none; outline: none; gridline-color: transparent;
  selection-background-color: {CONTROL_GRAY}; selection-color: {INK}; }}
QTableWidget::item {{ border-bottom: 1px solid {CONTROL_GRAY}; padding: 0 8px; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: transparent; color: {SLATE}; font-size: 12px; border: none;
  border-bottom: 1px solid {HAIRLINE}; padding: 6px 8px; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

QProgressBar {{ background: {CONTROL_GRAY}; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }}
QProgressBar::chunk {{ background: {PRICING_BLUE}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {HAIRLINE}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {STEEL}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {HAIRLINE}; border-radius: 3px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
  background: transparent; border: none; width: 0; height: 0; }}
QToolTip {{ background: {GALLERY_WHITE}; color: {INK}; border: 1px solid {HAIRLINE}; padding: 4px; }}

QDialog#studioDialog {{ background: transparent; }}
QFrame#dialogCard {{ background: {GALLERY_WHITE}; border: 1px solid {CONTROL_GRAY}; border-radius: 28px; }}
QLabel#dialogBadge {{ background: {CRITICAL}; color: {GALLERY_WHITE}; font-size: 14px; font-weight: 600;
  border-radius: 11px; min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px; }}
QTextEdit#dialogBody {{ background: transparent; color: {INK}; border: none;
  selection-background-color: {PRICING_BLUE}; selection-color: {GALLERY_WHITE}; }}
QDialog#studioDialog QPushButton {{ min-width: 48px; }}
QDialog#studioDialog QPushButton:focus {{ border-color: {PRICING_BLUE}; }}

QFileDialog {{ background: {GALLERY_WHITE}; }}
QFileDialog QTreeView#treeView, QFileDialog QListView#listView, QFileDialog QListView#sidebar {{
  background: {GALLERY_WHITE}; color: {INK}; border: 1px solid {CONTROL_GRAY}; border-radius: 10px; outline: none;
  selection-background-color: {CONTROL_GRAY}; selection-color: {INK}; }}
QFileDialog QListView#sidebar {{ background: {STUDIO_MIST}; }}
QFileDialog QToolButton {{ background: transparent; border: none; border-radius: 15px;
  min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; }}
QFileDialog QToolButton:hover, QFileDialog QToolButton:checked {{ background: {STUDIO_MIST}; }}
QFileDialog QToolButton:disabled {{ background: transparent; }}
"""


def apply_theme(app) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPalette

    app.setStyle("Fusion")
    # The design is light only; do not inherit a dark OS palette behind the QSS.
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPixelSize(14)
    app.setFont(font)
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, GALLERY_WHITE), (QPalette.ColorRole.Base, GALLERY_WHITE),
                        (QPalette.ColorRole.Text, INK), (QPalette.ColorRole.WindowText, INK),
                        (QPalette.ColorRole.ButtonText, INK), (QPalette.ColorRole.Button, GALLERY_WHITE),
                        (QPalette.ColorRole.PlaceholderText, STEEL), (QPalette.ColorRole.Highlight, PRICING_BLUE),
                        (QPalette.ColorRole.HighlightedText, GALLERY_WHITE)):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
