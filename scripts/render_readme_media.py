#!/usr/bin/env python3
"""Render the README images from real transcripts in docs/img/demos.json.

Needs `chromium` and `ffmpeg`. Writes into docs/img/:
- demo-1.png .. demo-3.png  one real question and answer each
- conectar.png              the three connection steps
- demo.gif                  animated replay of a real session (command, question, tools, answer)

Usage: python3 scripts/render_readme_media.py
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
URL = "https://indec-mcp-production.up.railway.app/mcp"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 17px/1.55 -apple-system, "Segoe UI", Roboto, "Noto Sans", Ubuntu, sans-serif;
       background: #eef1f5; color: #1c2330; padding: 28px; }
.card { background: #fff; border-radius: 16px; box-shadow: 0 6px 24px rgba(20,30,50,.10);
        padding: 26px 30px 20px; }
.q { display: flex; justify-content: flex-end; margin-bottom: 16px; }
.q span { background: #2f6fde; color: #fff; padding: 11px 17px; border-radius: 16px 16px 4px 16px;
          font-weight: 600; max-width: 85%; }
.tools { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
.tool { font: 13.5px/1.3 "JetBrains Mono", "DejaVu Sans Mono", Menlo, monospace; background: #f1f4f9;
        border: 1px solid #dbe2ee; color: #3b4a63; padding: 5px 10px; border-radius: 8px; }
.tool b { color: #1f5fcf; }
.a p { margin: 0 0 10px; }
.a strong { color: #0f1a2b; }
.foot { margin-top: 14px; padding-top: 12px; border-top: 1px solid #e6eaf1; font-size: 13px; color: #6a778c;
        display: flex; justify-content: space-between; gap: 16px; }
.badge { background: #e7f6ec; color: #1d7a3e; border-radius: 999px; padding: 2px 10px; font-weight: 600; }
"""


def md_inline(text: str) -> str:
    out = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)


def short_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={s if len(s) <= 46 else s[:43] + '…'}")
    return ", ".join(parts)


def chat_html(demo: dict, captured: str) -> str:
    tools = "".join(
        f'<span class="tool">🔧 <b>{html.escape(c["tool"])}</b>({html.escape(short_args(c["args"]))})</span>'
        for c in demo["tool_calls"]
    )
    paras = "".join(f"<p>{md_inline(p)}</p>" for p in demo["answer"].split("\n") if p.strip())
    return f"""<!doctype html><meta charset="utf-8"><style>{CSS}</style>
<div class="card">
  <div class="q"><span>{html.escape(demo["question"])}</span></div>
  <div class="tools">{tools}</div>
  <div class="a">{paras}</div>
  <div class="foot"><span><span class="badge">Respuesta real</span>&nbsp; Claude Code + servidor MCP <b>indec</b> · {captured}</span>
  <span>Datos: INDEC · servicio no oficial</span></div>
</div>"""


URL_WBR = URL.replace("//", "//<wbr>").replace(".", ".<wbr>").replace("/mcp", "<wbr>/mcp")
STEPS_HTML = f"""<!doctype html><meta charset="utf-8"><style>{CSS}
.steps {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }}
.step {{ background: #fff; border-radius: 16px; box-shadow: 0 6px 24px rgba(20,30,50,.10); padding: 22px 22px 20px; }}
.n {{ width: 38px; height: 38px; border-radius: 50%; background: #2f6fde; color: #fff; font-weight: 700;
      display: grid; place-items: center; font-size: 19px; margin-bottom: 12px; }}
h3 {{ font-size: 19px; margin-bottom: 6px; }}
.mono {{ font: 13.5px/1.5 "JetBrains Mono", "DejaVu Sans Mono", monospace; background: #f1f4f9;
         border: 1px solid #dbe2ee; border-radius: 8px; padding: 8px 10px; margin-top: 10px; }}
.ok {{ margin-top: 10px; background: #e7f6ec; color: #1d7a3e; border-radius: 8px; padding: 8px 10px; font-size: 15px; font-weight: 600; }}
.muted {{ color: #5d6a80; font-size: 15px; }}
</style>
<div class="steps">
  <div class="step"><div class="n">1</div><h3>Copiá esta URL</h3>
    <div class="muted">No hace falta registrarse ni pedir un token.</div>
    <div class="mono">{URL_WBR}</div></div>
  <div class="step"><div class="n">2</div><h3>Agregala como conector</h3>
    <div class="muted">En Claude, ChatGPT, Cursor o VS Code: "agregar servidor MCP / conector personalizado".</div>
    <div class="mono">claude mcp add --transport http indec<br>{URL_WBR}</div></div>
  <div class="step"><div class="n">3</div><h3>Preguntá en castellano</h3>
    <div class="muted">"¿Cómo viene la inflación?" · "¿Le ganaron los salarios?" · "¿Cuánto exportamos a Brasil?"</div>
    <div class="ok">✓ Cita "Fuente: INDEC", el período y si el dato es provisorio</div></div>
</div>"""


# -- animated terminal replay -------------------------------------------------------------------

TERM_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #eef1f5; padding: 22px; font: 15px/1.5 "JetBrains Mono", "DejaVu Sans Mono", Menlo, monospace; }
.win { background: #161b22; border-radius: 12px; box-shadow: 0 8px 28px rgba(0,0,0,.25); overflow: hidden; height: 650px;
       display: flex; flex-direction: column; }
.bar { background: #21262d; padding: 9px 14px; display: flex; gap: 7px; align-items: center; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.title { color: #8b949e; font-size: 12.5px; margin-left: 10px; }
.body { flex: 1; padding: 16px 20px; color: #c9d1d9; white-space: pre-wrap; overflow-wrap: anywhere;
        overflow: hidden; display: flex; flex-direction: column; justify-content: flex-end; }  /* newest lines stay visible */
.p { color: #7ee787; } .c { color: #e6edf3; } .o { color: #8b949e; } .u { color: #79c0ff; font-weight: 700; }
.t { color: #d2a8ff; } .a { color: #e6edf3; } .cur { background: #c9d1d9; color: #161b22; }
.cap { text-align: center; color: #5d6a80; font: 13px -apple-system, "Segoe UI", Roboto, sans-serif; margin-top: 9px; }
"""


def term_frame(lines: list[tuple[str, str]], cursor: bool) -> str:
    body = "".join(f'<span class="{cls}">{html.escape(txt)}</span>' for cls, txt in lines)
    if cursor:
        body += '<span class="cur"> </span>'
    return f"""<!doctype html><meta charset="utf-8"><style>{TERM_CSS}</style>
<div class="win"><div class="bar"><span class="dot" style="background:#ff5f56"></span>
<span class="dot" style="background:#ffbd2e"></span><span class="dot" style="background:#27c93f"></span>
<span class="title">terminal — Claude Code + indec MCP</span></div><div class="body"><div>{body}</div></div></div>
<div class="cap">Recreación animada de una sesión real (7 nov 2026). Comando, salida, herramientas y respuesta son textuales.</div>"""


def gif_frames(demo: dict) -> list[tuple[str, float]]:
    """(html, seconds) frames: type the command, show its real output, type the question, show the answer."""
    cmd = f"claude mcp add --scope project --transport http indec {URL}"
    added = f"Added HTTP MCP server indec with URL: {URL} to project config\n\n"
    q = demo["question"]
    frames: list[tuple[str, float]] = []
    base = [("p", "$ ")]
    for i in range(0, len(cmd) + 1, 7):
        frames.append((term_frame(base + [("c", cmd[:i])], True), 0.06))
    after_cmd = base + [("c", cmd + "\n"), ("o", added)]
    frames.append((term_frame(after_cmd, False), 1.4))
    ask = after_cmd + [("p", "$ "), ("c", 'claude "')]
    for i in range(0, len(q) + 1, 4):
        frames.append((term_frame(ask + [("u", q[:i])], True), 0.06))
    asked = ask + [("u", q), ("c", '"\n\n')]
    frames.append((term_frame(asked, False), 0.9))
    shown = list(asked)
    for c in demo["tool_calls"]:
        shown.append(("t", f"  ● indec · {c['tool']}({short_args(c['args'])})\n"))
        frames.append((term_frame(shown, False), 0.9))
    shown.append(("o", "\n"))
    answer = re.sub(r"\*\*(.+?)\*\*", r"\1", demo["answer"])
    paras = [p for p in answer.split("\n") if p.strip()]
    for p in paras:
        shown.append(("a", p + "\n\n"))
        frames.append((term_frame(shown, False), 1.6))
    frames.append((term_frame(shown, False), 5.0))
    return frames


# -- rendering ------------------------------------------------------------------------------------


def screenshot(html_text: str, out: Path, width: int, height: int, tmp: Path) -> None:
    page = tmp / (out.stem + ".html")
    page.write_text(html_text, encoding="utf-8")
    subprocess.run(
        [CHROMIUM, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
         "--force-device-scale-factor=2", f"--window-size={width},{height}",
         f"--screenshot={out}", page.as_uri()],
        check=True, capture_output=True,
    )


def autocrop(png: Path) -> None:
    """Trim the uniform page background below the card (ffmpeg cropdetect is overkill; use PIL if present)."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    im = Image.open(png).convert("RGB")
    bg = Image.new("RGB", im.size, im.getpixel((2, im.height - 2)))
    box = ImageChops.difference(im, bg).getbbox()
    if box:
        pad = 40
        im.crop((0, 0, im.width, min(im.height, box[3] + pad))).save(png, optimize=True)


def main() -> None:
    if not CHROMIUM or not shutil.which("ffmpeg"):
        raise SystemExit("needs chromium and ffmpeg")
    data = json.loads((IMG / "demos.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for i, demo in enumerate(data["demos"], 1):
            out = IMG / f"demo-{i}.png"
            screenshot(chat_html(demo, "7 nov 2026"), out, 900, 1100, tmp)
            autocrop(out)
            print("wrote", out.relative_to(ROOT))
        screenshot(STEPS_HTML, IMG / "conectar.png", 1100, 340, tmp)
        print("wrote docs/img/conectar.png")

        frames = gif_frames(data["demos"][1])  # exports to China
        listing = []
        for n, (page_html, secs) in enumerate(frames):
            png = tmp / f"f{n:03d}.png"
            page = tmp / f"f{n:03d}.html"
            page.write_text(page_html, encoding="utf-8")
            subprocess.run([CHROMIUM, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
                            "--force-device-scale-factor=1", "--window-size=920,730", f"--screenshot={png}",
                            page.as_uri()], check=True, capture_output=True)
            listing.append(f"file '{png}'\nduration {secs}\n")
        listing.append(f"file '{tmp / f'f{len(frames) - 1:03d}.png'}'\n")  # concat demuxer needs a final entry
        (tmp / "list.txt").write_text("".join(listing))
        gif = IMG / "demo.gif"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
             "-vf", "fps=12,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=none",
             "-loop", "0", str(gif)],
            check=True,
        )
        print("wrote", gif.relative_to(ROOT), f"({gif.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
