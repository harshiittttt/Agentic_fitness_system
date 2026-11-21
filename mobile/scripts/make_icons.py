"""Generates the app icon, Android adaptive icon and splash image.  Run: python mobile/scripts/make_icons.py
Design: the coach orb (lime -> mint gradient) with a voice waveform, on near-black. Drawn at 2x and downsampled for clean edges."""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(__file__), "..", "assets")
os.makedirs(OUT, exist_ok=True)
LIME, MINT, DARK, DARKER = (198, 255, 61), (52, 227, 164), (13, 15, 20), (7, 8, 11)


def orb(d: int) -> Image.Image:
    """Gradient orb with a waveform and a soft gloss, as RGBA of size d x d."""
    grad = Image.linear_gradient("L").resize((d, d)).rotate(35, resample=Image.BICUBIC)
    body = Image.composite(Image.new("RGB", (d, d), MINT), Image.new("RGB", (d, d), LIME), grad).convert("RGBA")
    # gloss: a blurred white ellipse, top-left
    gloss = Image.new("L", (d, d), 0)
    ImageDraw.Draw(gloss).ellipse([d * 0.12, d * 0.06, d * 0.62, d * 0.40], fill=70)
    gloss = gloss.filter(ImageFilter.GaussianBlur(d * 0.05))
    body.paste(Image.new("RGBA", (d, d), (255, 255, 255, 255)), (0, 0), gloss)
    # waveform bars (dark), vertically centred
    draw = ImageDraw.Draw(body)
    heights = [0.20, 0.38, 0.56, 0.38, 0.20]
    bw, gap = d * 0.075, d * 0.062
    total = len(heights) * bw + (len(heights) - 1) * gap
    x = (d - total) / 2
    for h in heights:
        hh = d * h
        draw.rounded_rectangle([x, (d - hh) / 2, x + bw, (d + hh) / 2], radius=bw / 2, fill=DARKER + (255,))
        x += bw + gap
    # circular mask, supersampled
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d * 4 - 1, d * 4 - 1], fill=255)
    mask = mask.resize((d, d), Image.LANCZOS)
    body.putalpha(ImageChops.multiply(body.getchannel("A"), mask))
    return body


def halo(size: int, radius: int, alpha: int) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c = size // 2
    ImageDraw.Draw(layer).ellipse([c - radius, c - radius, c + radius, c + radius], fill=LIME + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(size * 0.07))


def background(size: int) -> Image.Image:
    g = Image.linear_gradient("L").resize((size, size))
    return Image.composite(Image.new("RGB", (size, size), DARKER), Image.new("RGB", (size, size), DARK), g).convert("RGBA")


def compose(size: int, orb_frac: float, with_bg: bool, with_halo: bool) -> Image.Image:
    img = background(size) if with_bg else Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = int(size * orb_frac)
    if with_halo:
        img = Image.alpha_composite(img, halo(size, int(d * 0.62), 110))
    o = orb(d)
    img.alpha_composite(o, ((size - d) // 2, (size - d) // 2))
    return img


def save(img: Image.Image, name: str, size: int, flatten: bool = False) -> None:
    img = img.resize((size, size), Image.LANCZOS)
    if flatten:
        img = img.convert("RGB")
    img.save(os.path.join(OUT, name))
    print("wrote", name, img.size)


if __name__ == "__main__":
    S = 2048
    save(compose(S, 0.58, True, True), "icon.png", 1024, flatten=True)                 # iOS / store icon (no transparency)
    save(compose(S, 0.50, False, False), "adaptive-icon.png", 1024)                    # Android foreground; orb stays in the safe zone
    save(compose(S, 0.50, False, True), "splash.png", 1024)                             # shown on #07080B
    save(compose(S, 0.58, True, True), "favicon.png", 96, flatten=True)
