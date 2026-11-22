"""Recognize Chinese and English screenshot text using Apple's Vision framework."""
import json
import math
import sys

from Foundation import NSData
import objc
import Quartz
import Vision


def pixels(value):
    # Match Swift rounded() at half-pixel boundaries.
    return int(value + math.copysign(0.5, value))


def recognize(data):
    # Release native buffers after every frame, including in long-lived crawl workers.
    with objc.autorelease_pool():
        return recognize_image(data)


def recognize_image(data):
    source = Quartz.CGImageSourceCreateWithData(NSData.dataWithData_(data), None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
    if image is None:
        raise ValueError('无法读取输入图像')
    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(['zh-Hans', 'en-US'])
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise ValueError(str(error))
    lines = []
    for observation in request.results() or ():
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        best = candidates[0]
        rect = observation.boundingBox()
        lines.append(dict(text=str(best.string()), confidence=float(best.confidence()),
                          x=pixels(rect.origin.x * width), y=pixels((1 - rect.origin.y - rect.size.height) * height),
                          width=pixels(rect.size.width * width), height=pixels(rect.size.height * height)))
    return dict(width=width, height=height, lines=sorted(lines, key=lambda line: (line['y'], line['x'])))


if __name__ == '__main__':
    try:
        print(json.dumps(recognize(sys.stdin.buffer.read()), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        sys.exit(str(exc))
