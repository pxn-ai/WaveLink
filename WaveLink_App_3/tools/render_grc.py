"""Approximate picture of a .grc canvas (block boxes + wires) to check the layout.
    python3 tools/render_grc.py grc/cdma_phy.grc out.png [gnuradio_source_dir]
Block sizes are estimated from the visible parameters of each block definition."""
import glob
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
import yaml  # noqa: E402


def load_defs(src):
    defs = {}
    for f in glob.glob(os.path.join(src, "**", "*.block.yml"), recursive=True):
        try:
            d = yaml.safe_load(open(f))
            defs[d["id"]] = d
        except Exception:
            pass
    return defs


def visible_params(bid, params, defs):
    d = defs.get(bid)
    if bid == "epy_block":
        return [k for k in params if not k.startswith("_") and k not in
                ("comment", "affinity", "alias", "maxoutbuf", "minoutbuf")]
    if not d:
        return list(params)[:3]
    out = []
    for p in d.get("parameters", []):
        if p.get("hide", "none") in ("none",) and p.get("category", "") != "Advanced":
            out.append(p["id"])
    return out


def main(grc, png, src="/tmp/claude-0/gr"):
    defs = load_defs(src)
    fg = yaml.safe_load(open(grc))
    boxes = {}
    fig, ax = plt.subplots(figsize=(40, 30))
    for b in fg["blocks"]:
        x, y = b["states"]["coordinate"]
        vis = visible_params(b["id"], b["parameters"], defs)
        label = (defs.get(b["id"], {}).get("label") or b["id"]) if b["id"] != "epy_block" else "EPY"
        lines = [label] + [f"{k}: {str(b['parameters'].get(k, ''))[:24]}" for k in vis]
        w = max(130, 7.2 * max(len(s) for s in lines) + 20)
        h = 18 + 17 * len(lines)
        if b["id"] == "note":
            lines = ["NOTE: " + b["parameters"]["note"][:110]]
            w, h = 7.2 * len(lines[0]) + 20, 30
        boxes[b["name"]] = (x, y, w, h)
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=2",
                                    fc="#fffbe6" if b["id"] == "epy_block" else "#e8f0fe", ec="#555"))
        ax.text(x + 6, y + 14, "\n".join(lines), fontsize=6, va="top", family="monospace")
    for src_b, sp, dst_b, dp in fg["connections"]:
        if src_b in boxes and dst_b in boxes:
            x0, y0, w0, h0 = boxes[src_b]
            x1, y1, w1, h1 = boxes[dst_b]
            msg = not str(sp).isdigit()
            ax.plot([x0 + w0, x1], [y0 + h0 / 2, y1 + h1 / 2], color="#888" if msg else "#1a73e8",
                    lw=0.7, ls="--" if msg else "-")
    # overlap report
    names = list(boxes)
    over = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, c = boxes[names[i]], boxes[names[j]]
            if a[0] < c[0] + c[2] and c[0] < a[0] + a[2] and a[1] < c[1] + c[3] and c[1] < a[1] + a[3]:
                over.append((names[i], names[j]))
    xs = [v[0] + v[2] for v in boxes.values()]
    ys = [v[1] + v[3] for v in boxes.values()]
    ax.set_xlim(-20, max(xs) + 40)
    ax.set_ylim(max(ys) + 40, -20)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.savefig(png, dpi=60, bbox_inches="tight")
    print(f"{os.path.basename(grc)}: {len(boxes)} blocks, canvas {max(xs):.0f} x {max(ys):.0f}, "
          f"{len(over)} overlapping pairs")
    for a, c in over[:25]:
        print("   overlap:", a, "<->", c)


if __name__ == "__main__":
    main(*sys.argv[1:])
