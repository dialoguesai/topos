// Regenerate the blobs in attributed_body_blobs.py, on a Mac:
//
//     swift generate_fixtures.swift /tmp/imessage-fixtures
//
// then re-encode them into the Python module (zlib + base64, one entry per file).
//
// Every blob here is produced by Apple's own archivers -- NSArchiver for the
// "streamtyped" format and NSKeyedArchiver for "bplist00" -- over an
// NSAttributedString carrying Messages' own __kIM... attributes. That is the
// point of this file: the decoder is tested against the bytes Apple emits, not
// against our belief about them.
//
// ALL TEXT HERE IS INVENTED. Never seed a fixture from a real message.

import Foundation

let dir = URL(fileURLWithPath: CommandLine.arguments[1])

func emit(_ name: String, _ data: Data) {
    try! data.write(to: dir.appendingPathComponent(name))
    print("\(name): \(data.count) bytes")
}

/// An attributed string with the message-part attribute Messages always sets,
/// split into `parts` runs so multi-run archives are covered too.
func attr(_ s: String, parts: Int = 1) -> NSAttributedString {
    let m = NSMutableAttributedString(string: s)
    guard m.length > 0 else { return m }
    let chunk = max(1, m.length / max(1, parts))
    var loc = 0, idx = 0
    while loc < m.length {
        let len = min(chunk, m.length - loc)
        m.addAttributes([
            NSAttributedString.Key("__kIMMessagePartAttributeName"): NSNumber(value: idx),
            NSAttributedString.Key("__kIMBaseWritingDirectionAttributeName"): NSNumber(value: -1),
        ], range: NSRange(location: loc, length: len))
        loc += len
        idx += 1
    }
    return m
}

func both(_ name: String, _ text: String, parts: Int = 1) {
    let a = attr(text, parts: parts)
    emit("st_\(name).bin", NSArchiver.archivedData(withRootObject: a))
    emit("ka_\(name).bin", try! NSKeyedArchiver.archivedData(withRootObject: a, requiringSecureCoding: false))
}

// Ordinary bodies.
both("plain", "Hey are we still on for today")
both("unicode", "café ☕️ 我们明天见 👍🏽 naïve")
both("long", String(repeating: "the quick brown fox jumps over the lazy dog. ", count: 12)
    .trimmingCharacters(in: .whitespaces))
both("multirun", "first part here and second part there and a third", parts: 3)
both("multiline", "line one\nline two\n\nline four")
both("rtl", "مرحبا بالعالم")
both("tapback", "Liked “sounds good to me”")
both("plainstring", "just a plain string")

// Byte-length boundaries either side of each typedstream integer width: a
// 1-byte length below 127, a tagged 2-byte length above it, a tagged 4-byte
// length above 32767. A decoder reading the wrong width fails exactly here.
for n in [1, 31, 32, 126, 127, 128, 254, 255, 256, 32767, 32768, 65535, 65536, 70000] {
    both("len\(n)", String(repeating: "a", count: n))
}

// Bodies that mention the archive format, so a marker match alone cannot be the
// rule for "this is not a message".
both("selfref", "the word streamtyped appears here")
both("classref", "talking about $classname and $classes today")

// Nothing to recover: an empty body, and one that is only U+FFFC -- the
// placeholder Messages writes where an attachment sits.
emit("st_empty.bin", NSArchiver.archivedData(withRootObject: NSAttributedString(string: "")))
both("attach", "\u{FFFC}")
// A caption beside an attachment: the placeholder goes, the words stay.
both("mixed", "\u{FFFC}look at this photo")

// A data detector annotates a span, adding attribute objects around the body.
let detected = NSMutableAttributedString(string: "lets meet Sunday at noon, see https://example.com/x")
detected.addAttribute(NSAttributedString.Key("__kIMMessagePartAttributeName"),
                      value: NSNumber(value: 0),
                      range: NSRange(location: 0, length: detected.length))
detected.addAttribute(NSAttributedString.Key("__kIMLinkAttributeName"),
                      value: URL(string: "https://example.com/x")!,
                      range: NSRange(location: 30, length: 21))
detected.addAttribute(NSAttributedString.Key("__kIMDataDetectedAttributeName"),
                      value: NSValue(range: NSRange(location: 10, length: 6)),
                      range: NSRange(location: 10, length: 6))
emit("st_detected.bin", NSArchiver.archivedData(withRootObject: detected))
emit("ka_detected.bin", try! NSKeyedArchiver.archivedData(withRootObject: detected, requiringSecureCoding: false))
