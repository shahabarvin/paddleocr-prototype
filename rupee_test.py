"""Isolate the rupee-sign question: can PP-OCRv5 rec models read a clean
synthetic '₹360.00' crop? If yes, the receipt failures are a detection/crop
issue; if no, it's a recognition training limitation."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

out = Path("output/smoke")
out.mkdir(parents=True, exist_ok=True)
samples = []
for fname in ("segoeui.ttf", "arial.ttf"):
    for text in ("₹360.00", "₹ 360.00", "TOTAL: ₹3,769.50"):
        img = Image.new("RGB", (60 + 24 * len(text), 64), "white")
        d = ImageDraw.Draw(img)
        d.text((12, 8), text, font=ImageFont.truetype(fname, 40), fill="black")
        p = out / f"rupee_{fname.split('.')[0]}_{len(samples)}.png"
        img.save(p)
        samples.append((fname, text, p))

from paddleocr import TextRecognition

for model in ("PP-OCRv5_mobile_rec", "PP-OCRv5_server_rec"):
    try:
        m = TextRecognition(model_name=model, device="cpu", enable_mkldnn=False)
    except TypeError:
        m = TextRecognition(model_name=model, device="cpu")
    print(f"--- {model} ---")
    for fname, text, p in samples:
        r = list(m.predict(str(p)))[0]
        print(f"  {fname:12s} expected={text!r:26s} got={r['rec_text']!r} "
              f"(score {r['rec_score']:.2f})")
