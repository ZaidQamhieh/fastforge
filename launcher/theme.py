"""Design tokens, taken from the supplied Figma file rather than invented.

Backgrounds come from `meta.json`'s canvas colour and from decoding `thumbnail.png` -- the file's own
rendered preview -- and counting real pixels. Accents come from the same render, filtered to
saturated pixels, which is where the small bright elements live.

Sources, so a later change can be checked rather than guessed:
  canvas background   meta.json client_meta.background_color -> #1E1E1E
  surfaces            thumbnail pixel frequency (7098px #08090D, 1829px #10121A, 1631px #171A26)
  accents             saturated-pixel pass (#00FF87 green, #00E5FF cyan)
  states              #832034 / #95243A red, #833F1D amber
  type                "JetBrains Mono" string present in the document

JetBrains Mono is not installed on this machine, so `mono_family()` resolves the closest available
monospace instead of silently falling back to a proportional font.
"""
from __future__ import annotations

# Surfaces, darkest first.
BG_DEEP = "#08090D"      # app background, the dominant colour in the render
BG_ALT = "#0F1119"
BG_PANEL = "#10121A"     # sidebar and panels
BG_CARD = "#171A26"      # profile cards
BG_ELEVATED = "#1D212F"  # hover, inputs, elevated surfaces
BG_CANVAS = "#1E1E1E"    # Figma canvas colour

# Accents.
ACCENT = "#00FF87"       # primary green: play, success, active nav
ACCENT_DIM = "#0B8C56"
CYAN = "#00E5FF"         # secondary: stats, links, progress
CYAN_DIM = "#0B8092"

# States.
DANGER = "#95243A"
DANGER_BG = "#2B1018"
WARN = "#833F1D"

# Lines and text.
BORDER = "#1D212F"
BORDER_SOFT = "#363C4A"
TEXT = "#E8EAF0"
TEXT_DIM = "#8A90A2"
TEXT_FAINT = "#4D515B"

RADIUS = 10
RADIUS_SM = 6


def mono_family() -> str:
    """JetBrains Mono if present, otherwise the best installed monospace.

    The design specifies JetBrains Mono. Naming a missing family in a stylesheet makes Qt fall back
    to a proportional default, which changes every alignment in the layout, so the fallback is
    chosen explicitly here instead.
    """
    from PySide6.QtGui import QFontDatabase
    installed = set(QFontDatabase.families())
    for candidate in ("JetBrains Mono", "JetBrainsMono Nerd Font", "Fira Code", "Cascadia Code",
                      "Source Code Pro", "DejaVu Sans Mono", "Liberation Mono", "Monospace"):
        if candidate in installed:
            return candidate
    return "monospace"


def stylesheet(mono: str) -> str:
    return f"""
    QWidget {{ background: {BG_DEEP}; color: {TEXT}; font-family: "{mono}"; font-size: 13px; }}
    QLabel  {{ background: transparent; }}
    #sidebar {{ background: {BG_PANEL}; border-right: 1px solid {BORDER}; }}
    #logoText {{ color: {ACCENT}; font-size: 17px; font-weight: 700; letter-spacing: 1px; }}
    #logoSub  {{ color: {TEXT_FAINT}; font-size: 10px; letter-spacing: 1px; }}

    QPushButton#nav {{
        background: transparent; color: {TEXT_DIM}; border: none; border-radius: {RADIUS_SM}px;
        padding: 11px 14px; text-align: left; font-size: 13px;
    }}
    QPushButton#nav:hover {{ background: {BG_ELEVATED}; color: {TEXT}; }}
    QPushButton#nav:checked {{ background: {BG_ELEVATED}; color: {ACCENT}; font-weight: 700; }}

    #card {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}
    #card:hover {{ border: 1px solid {BORDER_SOFT}; }}
    #packIcon {{ background: {BG_ELEVATED}; border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS_SM}px; }}
    #cardTitle {{ font-size: 15px; font-weight: 700; }}
    #cardMeta  {{ color: {TEXT_DIM}; font-size: 11px; }}

    QPushButton#play {{
        background: {ACCENT}; color: #04160C; border: none; border-radius: {RADIUS_SM}px;
        padding: 10px 22px; font-weight: 700; font-size: 13px;
    }}
    QPushButton#play:hover {{ background: #4DFFAB; }}
    QPushButton#play:disabled {{ background: {ACCENT_DIM}; color: {TEXT_FAINT}; }}

    QPushButton#ghost {{
        background: transparent; color: {TEXT_DIM}; border: 1px solid {BORDER_SOFT};
        border-radius: {RADIUS_SM}px; padding: 9px 16px;
    }}
    QPushButton#ghost:hover {{ border-color: {CYAN}; color: {CYAN}; }}
    QPushButton#danger {{
        background: transparent; color: {DANGER}; border: 1px solid {DANGER};
        border-radius: {RADIUS_SM}px; padding: 9px 16px;
    }}
    QPushButton#danger:hover {{ background: {DANGER_BG}; }}

    QLineEdit {{
        background: {BG_DEEP}; border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS_SM}px;
        padding: 10px 12px; selection-background-color: {ACCENT_DIM};
    }}
    QLineEdit:focus {{ border-color: {ACCENT}; }}

    #badge     {{ color: {ACCENT}; font-size: 10px; letter-spacing: .6px; }}
    #badgeCyan {{ color: {CYAN};   font-size: 10px; letter-spacing: .6px; }}
    #badgeWarn {{ color: #E8A33D; font-size: 10px; letter-spacing: .6px; }}
    #sectionHeader {{ color: {TEXT_DIM}; font-size: 11px; letter-spacing: 1.4px; font-weight: 700; }}
    #metricValue {{ font-size: 22px; font-weight: 700; color: {ACCENT}; }}
    #metricLabel {{ color: {TEXT_DIM}; font-size: 10px; letter-spacing: .8px; }}

    QTextEdit {{ background: #05060A; border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;
                 color: #B9C0D0; font-size: 11px; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {BORDER_SOFT}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

    QSlider::groove:horizontal {{ background: {BG_ELEVATED}; height: 5px; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; height: 5px; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; height: 14px;
                                  margin: -5px 0; border-radius: 7px; }}
    QProgressBar {{ background: {BG_ELEVATED}; border: none; border-radius: 3px;
                    height: 6px; text-align: center; color: transparent; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
    """
