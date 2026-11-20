import Foundation
import Vision
import ImageIO
import CoreGraphics

struct VisibleText: Encodable {
    let text: String
    let confidence: Float
    let x: Int
    let y: Int
    let width: Int
    let height: Int
}
struct ScreenText: Encodable {
    let width: Int
    let height: Int
    let lines: [VisibleText]
}
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let source = CGImageSourceCreateWithData(data as CFData, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    FileHandle.standardError.write(Data("Could not read PNG.\n".utf8)); exit(1)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
let w = Double(image.width)
let h = Double(image.height)
let lines = (request.results ?? []).compactMap { result -> VisibleText? in
    guard let best = result.topCandidates(1).first else { return nil }
    let box = result.boundingBox
    return VisibleText(text: best.string, confidence: best.confidence,
        x: Int((box.minX * w).rounded()), y: Int(((1 - box.maxY) * h).rounded()),
        width: Int((box.width * w).rounded()), height: Int((box.height * h).rounded()))
}.sorted { ($0.y, $0.x) < ($1.y, $1.x) }
let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
FileHandle.standardOutput.write(try encoder.encode(ScreenText(width: image.width, height: image.height, lines: lines)))
