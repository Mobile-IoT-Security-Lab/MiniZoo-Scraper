// Local OCR. Pixel bounds use Appium's top-left origin. No network service.
// Build: swiftc tools/ocr.swift -o tools/ocr
// Use: ocr IMAGE [--min-confidence=0.8] [exact-label ...]
import Foundation
import ImageIO
import CoreGraphics
import Vision

func fail(_ message: String) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: ["error": message])
    FileHandle.standardError.write(data + Data("\n".utf8))
    exit(2)
}
struct TextBox {
    let text: String
    let confidence: Float
    let box: CGRect
}
func overlaps(_ first: CGRect, _ second: CGRect) -> Bool {
    let intersection = first.intersection(second)
    if intersection.isNull { return false }
    let area = intersection.width * intersection.height
    let union = first.width * first.height + second.width * second.height - area
    return union > 0 && area / union >= 0.5
}

let arguments = CommandLine.arguments
if arguments.count < 2 { fail("Usage: ocr IMAGE [--min-confidence=0.8] [exact-label ...]") }
let url = URL(fileURLWithPath: arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("Cannot read the image")
}
let options = arguments.dropFirst(2)
let thresholdOption = options.first { $0.hasPrefix("--min-confidence=") }
let threshold = thresholdOption.flatMap { Float($0.split(separator: "=").last ?? "") } ?? 0.8
if !threshold.isFinite || threshold < 0 || threshold > 1 { fail("Confidence must be between zero and one") }
let labels = Set(options.filter { !$0.hasPrefix("--min-confidence=") })

do {
    var recognized: [TextBox] = []
    // Dark-mode colored titles may be missed in the original. An additional
    // grayscale/inverted pass changes contrast only, never pixel geometry or
    // the source screenshot. Keep the same confidence threshold for all passes.
    let width = image.width, height = image.height
    let rowBytes = width * 4
    var pixels = [UInt8](repeating: 0, count: rowBytes * height)
    guard let contrastImage: CGImage = pixels.withUnsafeMutableBytes({ bytes in
        guard let context = CGContext(data: bytes.baseAddress, width: width, height: height,
                                      bitsPerComponent: 8, bytesPerRow: rowBytes,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                                        | CGBitmapInfo.byteOrder32Big.rawValue) else { return nil }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        for index in stride(from: 0, to: bytes.count, by: 4) {
            let gray = (299 * Int(bytes[index]) + 587 * Int(bytes[index + 1])
                        + 114 * Int(bytes[index + 2])) / 1000
            let enhanced = max(0, min(255, (gray - 128) * 3 / 2 + 128))
            let inverted = UInt8(255 - enhanced)
            bytes[index] = inverted
            bytes[index + 1] = inverted
            bytes[index + 2] = inverted
        }
        return context.makeImage()
    }) else { fail("Cannot prepare contrast image") }
    // A high-contrast green-channel pass keeps highlighted search titles
    // legible on the dark background. Coordinates and screenshot stay intact.
    var greenPixels = [UInt8](repeating: 0, count: rowBytes * height)
    guard let greenImage: CGImage = greenPixels.withUnsafeMutableBytes({ bytes in
        guard let context = CGContext(data: bytes.baseAddress, width: width, height: height,
                                      bitsPerComponent: 8, bytesPerRow: rowBytes,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                                        | CGBitmapInfo.byteOrder32Big.rawValue) else { return nil }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        for index in stride(from: 0, to: bytes.count, by: 4) {
            let value = UInt8(255 - max(0, min(255, (Int(bytes[index + 1]) - 40) * 255 / 80)))
            bytes[index] = value
            bytes[index + 1] = value
            bytes[index + 2] = value
        }
        return context.makeImage()
    }) else { fail("Cannot prepare green-channel image") }
    for input in [image, contrastImage, greenImage] {
        for languages in [["en-US", "zh-Hans"], ["zh-Hans", "en-US"]] {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = languages
            request.usesLanguageCorrection = false
            request.usesCPUOnly = true
            try VNImageRequestHandler(cgImage: input).perform([request])
            for observation in request.results ?? [] {
                guard let candidate = observation.topCandidates(1).first,
                      candidate.confidence >= threshold,
                      labels.isEmpty || labels.contains(candidate.string) else { continue }
                let item = TextBox(text: candidate.string, confidence: candidate.confidence,
                                   box: observation.boundingBox)
                if let index = recognized.firstIndex(where: { $0.text == item.text && overlaps($0.box, item.box) }) {
                    if item.confidence > recognized[index].confidence { recognized[index] = item }
                } else {
                    recognized.append(item)
                }
            }
        }
    }
    let pixelWidth = Double(image.width), pixelHeight = Double(image.height)
    let nodes: [[String: Any]] = recognized.map { item in
        let box = item.box
        let bounds = [Int(floor(box.minX * pixelWidth)), Int(floor((1 - box.maxY) * pixelHeight)),
                      Int(ceil(box.maxX * pixelWidth)), Int(ceil((1 - box.minY) * pixelHeight))]
        return ["text": item.text, "confidence": item.confidence, "bounds": bounds]
    }
    let result: [String: Any] = ["width": image.width, "height": image.height, "nodes": nodes]
    let output = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(output + Data("\n".utf8))
} catch {
    fail("Apple Vision OCR failed: " + error.localizedDescription)
}
