#!/usr/bin/env python3
#
# Project EBCDIC codepages registered as Python codecs.
# 
# Importing this module (which ap101Utils/__init__.py does) registers:
# 
#   codec "ebcdicvagc"          the virtualagc/AP-101 EBCDIC translation
#   error handler "ebcdicblank" unencodable characters become EBCDIC blanks (0x40)
# 
# "ebcdicvagc" is NOT cp037.  The tables in asciiToEbcdic.py convert
#           `~`/`^` -> 0x5F NOT-sign, 
#           backtick -> 0x4A cent,
#           `\\` -> 0xFE 
# and decode every byte with no ASCII stand-in to a space, so the
# encode and decode directions are deliberately not inverses of each other.
# 
# Only characters 0x00-0x7F are encodable; anything else raises
# UnicodeEncodeError under "strict" or becomes 0x40 under "ebcdicblank".
# "ebcdicblank" is an encode-direction handler only (decoding never errors:
# all 256 byte values are defined).
# 
#
# We're not encoding the extra DEU characters here yet; we'll integrate that 
# later.
#
import codecs

from .asciiToEbcdic import asciiToEbcdic, ebcdicToAscii


def _make_codec(name, encoding_map, decoding_table):
    """A CodecInfo for an asymmetric charmap codepage."""

    def encode(input, errors="strict"):
        return codecs.charmap_encode(input, errors, encoding_map)

    def decode(input, errors="strict"):
        return codecs.charmap_decode(input, errors, decoding_table)

    class IncrementalEncoder(codecs.IncrementalEncoder):
        def encode(self, input, final=False):
            return codecs.charmap_encode(input, self.errors, encoding_map)[0]

    class IncrementalDecoder(codecs.IncrementalDecoder):
        def decode(self, input, final=False):
            return codecs.charmap_decode(input, self.errors, decoding_table)[0]

    class Codec(codecs.Codec):
        def encode(self, input, errors="strict"):
            return encode(input, errors)

        def decode(self, input, errors="strict"):
            return decode(input, errors)

    class StreamWriter(Codec, codecs.StreamWriter):
        pass

    class StreamReader(Codec, codecs.StreamReader):
        pass

    return codecs.CodecInfo(
        name=name,
        encode=encode,
        decode=decode,
        incrementalencoder=IncrementalEncoder,
        incrementaldecoder=IncrementalDecoder,
        streamreader=StreamReader,
        streamwriter=StreamWriter,
    )


_CODEPAGES = {
    "ebcdicvagc": _make_codec(
        "ebcdicvagc",
        {i: asciiToEbcdic[i] for i in range(128)},
        "".join(ebcdicToAscii),
    ),
}


def _search(name):
    return _CODEPAGES.get(name.replace("_", ""))


def _blank_error(exc):
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    return b"\x40" * (exc.end - exc.start), exc.end


codecs.register(_search)
codecs.register_error("ebcdicblank", _blank_error)
