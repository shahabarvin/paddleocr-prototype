"""End-to-end smoke test: generates a synthetic document image (heading,
two columns, table), runs it through OCRService, prints timings + markdown.
First run triggers the model download. Usage: python smoke_test.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def make_test_image(path: Path) -> None:
    img = Image.new("RGB", (1400, 1000), "white")
    d = ImageDraw.Draw(img)

    def font(size):
        for name in ("arialbd.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    h1, body, bold = font(48), font(26), font(30)

    d.text((80, 50), "Quarterly Operations Report", font=h1, fill="black")
    d.line((80, 130, 1320, 130), fill="black", width=3)

    left = [
        "The logistics division processed",
        "48,200 shipments in Q2, an increase",
        "of 12 percent over the previous",
        "quarter. Average delivery time fell",
        "from 3.1 days to 2.6 days.",
    ]
    right = [
        "Customer satisfaction reached 94",
        "percent, driven by faster support",
        "response times. Refund requests",
        "declined by 8 percent compared",
        "with the first quarter.",
    ]
    d.text((80, 170), "Logistics", font=bold, fill="black")
    d.text((760, 170), "Customer Service", font=bold, fill="black")
    for i, line in enumerate(left):
        d.text((80, 230 + i * 40), line, font=body, fill="black")
    for i, line in enumerate(right):
        d.text((760, 230 + i * 40), line, font=body, fill="black")

    d.text((80, 480), "Key Metrics", font=bold, fill="black")
    rows = [
        ("Metric", "Q1", "Q2"),
        ("Shipments", "43,000", "48,200"),
        ("Avg. delivery (days)", "3.1", "2.6"),
        ("Satisfaction (%)", "91", "94"),
    ]
    x0, y0, col_w, row_h = 80, 540, 320, 60
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            d.rectangle(
                (x0 + c * col_w, y0 + r * row_h,
                 x0 + (c + 1) * col_w, y0 + (r + 1) * row_h),
                outline="black", width=2,
            )
            d.text((x0 + c * col_w + 14, y0 + r * row_h + 14),
                   cell, font=body, fill="black")
    img.save(path)


if __name__ == "__main__":
    from app import OCRService

    here = Path(__file__).parent
    test_img = here / "output" / "smoke" / "test_doc.png"
    test_img.parent.mkdir(parents=True, exist_ok=True)
    make_test_image(test_img)
    print(f"Test image written to {test_img}")

    svc = OCRService()
    print("Loading pipeline (first run downloads models)...")
    load_s = svc.load()
    print(f"Model load: {load_s:.1f}s ({svc.load_meta()})")

    result = svc.process(test_img, viz_dir=test_img.parent / "viz")
    print(f"\nTimings: {result['timings']}")
    print(f"Pages: {result['pages']}")
    print(f"Visualizations: {len(result['visualizations'])} file(s)")
    print("\n----- MARKDOWN -----\n")
    print(result["markdown"])
