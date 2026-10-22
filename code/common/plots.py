import os
import subprocess
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from common.config import CODE_DIR, FIGURES_DIR

BLUE = "#1f77b4"
ORANGE = "#ff7f0e"
GREEN = "#2ca02c"
RED = "#d62728"
PURPLE = "#9467bd"
GREY = "#7f7f7f"
LIGHT_GREY = "#c7c7c7"
FAULT_SHADE = "#fbe0dd"
LIVELOCK_SHADE = "#f9cdc8"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.labelsize": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": "#e5e5e5",
    "grid.linewidth": 0.8,
    "legend.fontsize": 9,
    "legend.frameon": False,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "lines.linewidth": 1.8,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
})


def open_file(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def save_figure(fig, name, show=False):
    if not os.path.exists(FIGURES_DIR):
        os.makedirs(FIGURES_DIR)
    path = os.path.join(FIGURES_DIR, name)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("Figure: %s" % os.path.relpath(path, CODE_DIR))
    if show:
        open_file(path)
    return path


def fault_span(manifest):
    start = None
    end = None
    for phase in manifest["phases"]:
        if phase["name"] in ("ramp", "fault"):
            if start is None:
                start = phase["start"]
            end = phase["end"]
    return start, end


def mark_phases(ax, manifest, label=False):
    t0 = manifest["start"]
    start, end = fault_span(manifest)
    if start is not None:
        ax.axvspan(start - t0, end - t0, color=FAULT_SHADE, linewidth=0, zorder=0)
    for phase in manifest["phases"][1:]:
        ax.axvline(phase["start"] - t0, color=LIGHT_GREY, linestyle=":", linewidth=1, zorder=1)
    if label:
        for phase in manifest["phases"]:
            middle = (phase["start"] + phase["end"]) / 2.0 - t0
            ax.text(middle, 1.02, phase["name"], transform=ax.get_xaxis_transform(), ha="center",
                    va="bottom", fontsize=9, color=GREY)
