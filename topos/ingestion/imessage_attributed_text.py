"""Bounded backing-text decoding for native iMessage evidence.

No Objective-C objects are instantiated. The typedstream reader recognizes only
the exact Foundation root/string class prefixes; the keyed reader follows the
declared root and NSString reference. Attribute strings never become body text.
Unlike the sync display decoder, this preserves whitespace and refuses object
replacement characters instead of dropping attachment placeholders.
"""
import plistlib
import time

from .owner_snapshot import SnapshotRejected

MAX_ARCHIVE_BYTES = 256 * 1024
MAX_TEXT_BYTES = 64 * 1024
_HEADER = b'\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84'
_ROOTS = (
    (b'\x19NSMutableAttributedString\x00\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84', b'\x95'),
    (b'\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84', b'\x94'),
)
_STRINGS = (b'\x0fNSMutableString\x01\x84\x84\x08NSString\x01', b'\x08NSString\x01')
_PREFIXES = tuple(_HEADER + root + string + ref + b'\x84\x01+'
                  for root, ref in _ROOTS for string in _STRINGS)


def _reject():
    raise SnapshotRejected('snapshot_attributed_text_unsupported')


def _typed(raw):
    prefix = next((value for value in _PREFIXES if raw.startswith(value)), None)
    if prefix is None or len(raw) <= len(prefix):
        _reject()
    pos = len(prefix)
    tag = raw[pos]
    pos += 1
    if tag in (0x81, 0x82, 0x83):
        width = {0x81: 2, 0x82: 4, 0x83: 8}[tag]
        if pos + width > len(raw):
            _reject()
        length = int.from_bytes(raw[pos:pos + width], 'little', signed=True)
        pos += width
    else:
        length = int.from_bytes(bytes((tag,)), 'little', signed=True)
    if not 0 < length <= MAX_TEXT_BYTES or pos + length + 2 > len(raw):
        _reject()
    # The backing NSString closes before the attributed-string ranges. Ranges
    # remain opaque data; they cannot redirect which bytes supply the body.
    if raw[pos + length] != 0x86 or raw[-1] != 0x86:
        _reject()
    # Validate the entire archive, including the attribute framing. Parsing is
    # pure Python: archived class names never instantiate Objective-C objects.
    # This catches truncated tails that a backing-string-only decoder misses.
    from typedstream.stream import (TypedStreamReader, BeginTypedValues, EndTypedValues,
        BeginObject, EndObject, BeginArray, EndArray, BeginStruct, EndStruct)
    beginnings = (BeginTypedValues, BeginObject, BeginArray, BeginStruct)
    endings = (EndTypedValues, EndObject, EndArray, EndStruct)
    depth, roots = 0, 0
    deadline = time.monotonic() + 0.1
    with TypedStreamReader.from_data(raw) as reader:
        for count, event in enumerate(reader, 1):
            if count > 8192 or time.monotonic() > deadline:
                _reject()
            if isinstance(event, beginnings):
                if depth == 0:
                    roots += 1
                depth += 1
                if depth > 64 or roots > 1:
                    _reject()
            elif isinstance(event, endings):
                depth -= 1
                if depth < 0:
                    _reject()
        if depth or roots != 1:
            _reject()
    return raw[pos:pos + length].decode('utf-8', errors='strict')


def _keyed(raw):
    archive = plistlib.loads(raw)
    if (type(archive) is not dict or set(archive) != {'$archiver', '$version', '$top', '$objects'}
            or archive['$archiver'] != 'NSKeyedArchiver' or archive['$version'] != 100000
            or type(archive['$top']) is not dict or set(archive['$top']) != {'root'}):
        _reject()
    objects = archive['$objects']
    if type(objects) is not list or not 1 <= len(objects) <= 1024:
        _reject()

    def deref(value):
        if type(value) is not plistlib.UID or not 0 <= value.data < len(objects):
            _reject()
        return objects[value.data]

    def class_is(node, names):
        cls = deref(node.get('$class'))
        return (type(cls) is dict and set(cls) == {'$classname', '$classes'}
                and cls.get('$classname') in names and cls.get('$classes') == names[cls['$classname']])

    root = deref(archive['$top']['root'])
    if (type(root) is not dict or not {'$class', 'NSString'} <= set(root)
            or not set(root) <= {'$class', 'NSString', 'NSAttributes', 'NSAttributeInfo'}
            or not class_is(root, {
                'NSAttributedString': ['NSAttributedString', 'NSObject'],
                'NSMutableAttributedString': ['NSMutableAttributedString', 'NSAttributedString', 'NSObject'],
            })):
        _reject()
    text = deref(root['NSString'])
    if type(text) is str:
        return text
    if (type(text) is dict and set(text) == {'$class', 'NS.string'}
            and class_is(text, {'NSString': ['NSString', 'NSObject'],
                'NSMutableString': ['NSMutableString', 'NSString', 'NSObject']})):
        return text['NS.string']
    _reject()


def decode_attributed_text(raw):
    """Return the exact text, or a content-free refusal. This proves no authorship."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
        _reject()
    try:
        text = _keyed(raw) if raw.startswith(b'bplist00') else _typed(raw)
        if (type(text) is not str or not text.strip() or '\x00' in text or '\ufffc' in text
                or len(text.encode('utf-8')) > MAX_TEXT_BYTES):
            _reject()
        return text
    except SnapshotRejected:
        raise
    except Exception:
        _reject()


def has_text_besides_attachments(raw):
    """Count-only: whether an attributed body holds characters other than attachment placeholders.

    True, False, or None when the body cannot be read. It never returns or logs the text, and it
    proves nothing: `decode_attributed_text` still refuses any body with an attachment in it.
    It exists so a native census can tell a captioned attachment from a bare one.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
        return None
    try:
        text = _keyed(raw) if raw.startswith(b'bplist00') else _typed(raw)
    except Exception:  # noqa: BLE001 -- count-only: an unreadable body is "unmeasured", never an error
        return None
    if type(text) is not str:
        return None
    return bool(text.replace('\ufffc', '').strip())
