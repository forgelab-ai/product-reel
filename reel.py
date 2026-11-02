"""product-reel: one product photo plus a few words in, a 1080x1920 promo Reel out.

The photo slowly zooms over a blurred copy of itself, the title drops in, features appear one by one,
then the price and a call to action. Built for small shops that want a Reel per product without editing.

    python reel.py photo.jpg "ThinkPad T14 Gen 3" --price "$649" --features "i7 / 16 GB" "512 GB SSD" --cta "DM to order"
    python reel.py --serve            # HTTP service for n8n: POST /reel with JSON, returns video/mp4
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:  # Persian/Arabic text: Pillow without libraqm needs the letters joined and reordered by hand
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    arabic_reshaper = None

W, H, FPS = 1080, 1920, 30
SECONDS = 11
FONTS = [r"C:\Windows\Fonts\segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
ACCENT = (255, 122, 47)


def ffmpeg():
    for d in (os.environ.get("FFMPEG_DIR"), None,
              Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"):
        p = shutil.which("ffmpeg", path=str(d)) if d else shutil.which("ffmpeg")
        if p:
            return p
    sys.exit("ffmpeg not found: install it or set FFMPEG_DIR")


def font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


RTL = re.compile("[؀-ۿ]")


def vis(text):
    """The line as it should be drawn: right-to-left scripts joined and ordered for display."""
    if arabic_reshaper and RTL.search(text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def wrap(draw, text, f, width):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(vis(trial), font=f) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def layer(draw_fn):
    img = Image.new("RGBA", (W, H))
    draw_fn(ImageDraw.Draw(img))
    return img


def background(photo):
    """Blurred, darkened cover-fit copy of the photo for the full frame."""
    bg = ImageOps.fit(photo, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    return Image.blend(bg, Image.new("RGB", (W, H), (10, 10, 12)), 0.45)


def foreground(photo):
    """The sharp photo on a white rounded card, sized for the middle of the frame."""
    card_w = 900
    p = ImageOps.contain(photo, (card_w - 60, 600), Image.LANCZOS)  # keeps the card clear of the feature pills
    card = Image.new("RGBA", (card_w, p.height + 60), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([0, 0, card_w - 1, card.height - 1], radius=40, fill=(255, 255, 255, 255))
    card.paste(p, ((card_w - p.width) // 2, 30))
    return card


def overlays(title, price, features, cta):
    """Transparent full-frame PNG layers and the second each one appears."""
    out = []

    def title_layer(d):
        f = font(78)
        y = 170
        for line in wrap(d, title, f, W - 140)[:3]:
            line = vis(line)
            tw = d.textlength(line, font=f)
            d.text(((W - tw) / 2, y), line, font=f, fill="white", stroke_width=5, stroke_fill=(0, 0, 0))
            y += 92
    out.append((layer(title_layer), 0.4))

    f = font(50)
    for i, feat in enumerate(features[:4]):
        def feat_layer(d, i=i, feat=vis(feat)):
            tw = d.textlength(feat, font=f)
            x, y = (W - tw - 80) / 2, 1240 + i * 92
            d.rounded_rectangle([x, y, x + tw + 80, y + 78], radius=39, fill=(15, 15, 18, 225))
            d.ellipse([x + 26, y + 31, x + 42, y + 47], fill=ACCENT)
            d.text((x + 56, y + 9), feat, font=f, fill="white")
        out.append((layer(feat_layer), 2.0 + i * 0.9))

    if price:
        def price_layer(d, price=vis(price)):
            size = 96
            while size > 48 and d.textlength(price, font=font(size)) > W - 220:  # long text like "ask in DM"
                size -= 4
            pf = font(size)
            tw = d.textlength(price, font=pf)
            x, y = (W - tw - 100) / 2, 1580
            d.rounded_rectangle([x, y, x + tw + 100, y + 140], radius=70, fill=ACCENT)
            d.text((x + 50, y + 70), price, font=pf, fill=(20, 12, 5), anchor="lm")
        out.append((layer(price_layer), 2.0 + len(features[:4]) * 0.9 + 0.4))

    if cta:
        def cta_layer(d, cta=vis(cta)):
            cf = font(58)
            tw = d.textlength(cta, font=cf)
            d.rectangle([0, H - 150, W, H], fill=(0, 0, 0, 170))
            d.text(((W - tw) / 2, H - 120), cta, font=cf, fill=(255, 210, 60))
        out.append((layer(cta_layer), SECONDS - 3.2))
    return out


def make_reel(image_bytes, title, price="", features=(), cta=""):
    photo = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    work = Path(tempfile.mkdtemp(prefix="reel-"))
    try:
        background(photo).save(work / "bg.png")
        card = foreground(photo)
        card.save(work / "card.png")
        layers = overlays(title, price, list(features), cta)
        inputs = ["-loop", "1", "-t", str(SECONDS), "-i", str(work / "bg.png"),
                  "-loop", "1", "-t", str(SECONDS), "-i", str(work / "card.png")]
        for i, (img, _) in enumerate(layers):
            img.save(work / f"o{i}.png")
            inputs += ["-loop", "1", "-t", str(SECONDS), "-i", str(work / f"o{i}.png")]
        n = SECONDS * FPS
        # The card grows 0-6% with an ease-out, sitting a little above centre.
        z = f"(1+0.06*(1-pow(1-n/{n},2)))"
        g = (f"[1:v]format=rgba,scale=w='trunc({card.width}*{z}/2)*2':h='trunc({card.height}*{z}/2)*2':eval=frame[card];"
             f"[0:v][card]overlay=x='(W-w)/2':y='(H-h)/2-80':eval=frame[v0]")
        last = "v0"
        for i, (_, t0) in enumerate(layers):
            g += (f";[{i + 2}:v]format=rgba,fade=t=in:st={t0}:d=0.35:alpha=1[l{i}]"
                  f";[{last}][l{i}]overlay=0:0:enable='gte(t,{t0})'[v{i + 1}]")
            last = f"v{i + 1}"
        g += f";[{last}]fade=t=in:d=0.3,fade=t=out:st={SECONDS - 0.4}:d=0.4,format=yuv420p[out]"
        out = work / "reel.mp4"
        r = subprocess.run([ffmpeg(), "-v", "error", "-y", *inputs, "-filter_complex", g, "-map", "[out]",
                            "-r", str(FPS), "-t", str(SECONDS), "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                            "-movflags", "+faststart", str(out)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-1200:])
        return out.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    """POST /reel {"image_base64", "title", "price", "features": [...] or "a\\nb", "cta"} -> video/mp4."""

    def do_POST(self):
        if self.path != "/reel":
            return self.send_error(404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            feats = body.get("features") or []
            if isinstance(feats, str):
                feats = [x.strip() for x in feats.replace(",", "\n").splitlines() if x.strip()]
            video = make_reel(base64.b64decode(body["image_base64"]), body["title"], body.get("price", ""),
                              feats, body.get("cta", ""))
        except Exception as e:  # report the reason to n8n instead of a bare 500
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            return self.wfile.write(msg)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition", 'attachment; filename="reel.mp4"')
        self.send_header("Content-Length", str(len(video)))
        self.end_headers()
        self.wfile.write(video)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photo", nargs="?")
    ap.add_argument("title", nargs="?")
    ap.add_argument("--price", default="")
    ap.add_argument("--features", nargs="*", default=[])
    ap.add_argument("--cta", default="")
    ap.add_argument("--out", default="reel.mp4")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8777)
    a = ap.parse_args()
    if a.serve:
        print(f"product-reel listening on http://127.0.0.1:{a.port}/reel")
        ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
    if not (a.photo and a.title):
        ap.error("photo and title are required (or use --serve)")
    Path(a.out).write_bytes(make_reel(Path(a.photo).read_bytes(), a.title, a.price, a.features, a.cta))
    print(a.out)


if __name__ == "__main__":
    main()
