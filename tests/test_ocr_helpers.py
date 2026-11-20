"""Unit tests for OCRService pure helpers (cv2/numpy only — no paddle/GPU)."""
import numpy as np

from app import OCRService


def test_normalize_fullwidth():
    assert OCRService._normalize_fullwidth("Receipt #：1") == "Receipt #:1"
    assert OCRService._normalize_fullwidth("ＡＢ１２") == "AB12"
    assert OCRService._normalize_fullwidth("a　b") == "a b"          # ideographic space
    assert OCRService._normalize_fullwidth("normal") == "normal"


def test_is_noise():
    assert OCRService._is_noise("+++***+*") is True
    assert OCRService._is_noise("---") is False                          # page break, kept
    assert OCRService._is_noise("abc") is False
    assert OCRService._is_noise("$5.00") is False
    assert OCRService._is_noise("**") is False                           # < 3 chars


def test_strip_noise_lines():
    out = OCRService._strip_noise_lines("Hello\n+++***\n\n---\nWorld")
    lines = out.split("\n")
    assert "Hello" in lines and "World" in lines
    assert "+++***" not in out
    assert "---" in lines                                                # page break kept
    assert "" in lines                                                   # blank kept


def test_normalize_device():
    assert OCRService._normalize_device("gpu") == "gpu"
    assert OCRService._normalize_device("GPU:0") == "gpu"
    assert OCRService._normalize_device("cuda") == "gpu"
    assert OCRService._normalize_device("cpu") == "cpu"
    assert OCRService._normalize_device("") == "gpu"
    assert OCRService._normalize_device("auto") == "gpu"
    assert OCRService._normalize_device("bogus") == "cpu"


def test_resolve_device_falls_back(monkeypatch):
    svc = OCRService(device="gpu")
    monkeypatch.setattr(OCRService, "gpu_available", staticmethod(lambda: False))
    eff, note = svc._resolve_device("gpu")
    assert eff == "cpu" and note                                          # note explains fallback
    eff2, note2 = svc._resolve_device("cpu")
    assert eff2 == "cpu" and note2 is None


def test_resolve_device_gpu_available(monkeypatch):
    svc = OCRService(device="gpu")
    monkeypatch.setattr(OCRService, "gpu_available", staticmethod(lambda: True))
    eff, note = svc._resolve_device(None)                                 # default gpu
    assert eff == "gpu" and note is None


def test_enhance_image_upscales_small():
    img = np.full((50, 80, 3), 127, dtype=np.uint8)
    out = OCRService._enhance_image(img)
    assert out.ndim == 3 and out.shape[2] == 3
    assert out.shape[:2] == (100, 160)                                   # small -> 2x


def test_enhance_image_binarize():
    img = np.full((2000, 100, 3), 127, dtype=np.uint8)                   # tall -> not upscaled
    out = OCRService._enhance_image(img, binarize=True)
    assert out.shape[:2] == (2000, 100)
    assert set(np.unique(out)).issubset({0, 255})                        # binary output


def test_poly_to_bbox():
    bbox = OCRService._poly_to_bbox([(10, 20), (30, 20), (30, 40), (10, 40)])
    assert bbox == (10.0, 20.0, 30.0, 40.0)


def test_has_text():
    assert OCRService._has_text("From Account 00021473851") is True
    assert OCRService._has_text('<div><img src="x.jpg"/></div>') is False   # image only
    assert OCRService._has_text("") is False


def test_enough_text():
    assert OCRService._enough_text("a lot of real recognized text here now") is True
    assert OCRService._enough_text("ab") is False                            # near-blank


def test_dropped_tokens_detects_lost_field():
    # markdown kept the label but lost the account value that OCR did read.
    md = "From Account\n\nAmount\n\nCAD 734.50"
    lt = "From Account   00021473851-AXLEC0RP-CAD\nAmount   CAD 734.50"
    dropped = OCRService._dropped_tokens(md, lt)
    assert "00021473851" in dropped and "axlec0rp" in dropped
    assert len(dropped) >= 2                                                 # triggers fallback


def test_dropped_tokens_clean_markdown_no_false_positive():
    lt = "From Account 00021473851 AXLE CORP CAD Amount 734.50 Reference 998877"
    assert OCRService._dropped_tokens(lt, lt) == []                          # identical -> none
    # substring match tolerates markdown merging words together
    assert OCRService._dropped_tokens("Download summary now", "Download summary") == []


def test_extract_lines():
    res = {"overall_ocr_res": {
        "rec_texts": ["From Account", "00021473851-AXLEC0RP-CAD"],
        "rec_scores": [0.991, 0.975],
        "rec_boxes": [[10, 20, 100, 30], [190, 320, 349, 331]],
    }}
    pages = OCRService._extract_lines([res])
    assert len(pages) == 1 and len(pages[0]) == 2
    val = pages[0][1]
    assert val["text"] == "00021473851-AXLEC0RP-CAD"
    assert val["box"] == [190.0, 320.0, 349.0, 331.0]
    assert val["score"] == 0.975


def test_extract_lines_falls_back_to_polys():
    res = {"overall_ocr_res": {
        "rec_texts": ["hi"], "rec_scores": [0.8], "rec_boxes": None,
        "rec_polys": [[(5, 6), (20, 6), (20, 15), (5, 15)]],
    }}
    line = OCRService._extract_lines([res])[0][0]
    assert line["box"] == [5.0, 6.0, 20.0, 15.0]            # bbox from polygon
