"""Real `attributedBody` blobs, produced by Apple's own archivers.

Generated on macOS by `generate_fixtures.swift`, next to this file: every blob is
the output of `NSArchiver.archivedData` (the "streamtyped" format) or
`NSKeyedArchiver.archivedData` (the "bplist00" format) over an
`NSAttributedString` carrying Messages' own `__kIM...` attributes. They are not
bytes this repo hand-rolled, so the decoder is tested against the format Apple
actually emits rather than against our belief about it.

Every string is invented -- no fixture is seeded from a real message. Blobs are
zlib-compressed and base64-encoded to keep the module small; the length-boundary
cases are 64KB of one character.
"""

import base64
import zlib
from typing import Optional


def _blob(encoded: str) -> bytes:
    """Decompress one stored fixture back to the exact bytes Apple wrote."""
    return zlib.decompress(base64.b64decode(encoded))


# name -> (blob, text the decoder must recover, or None when there is none)
ATTRIBUTED_BODY_FIXTURES: dict[str, tuple[bytes, Optional[str]]] = {

    "typedstream_plain": (
        _blob(
            "eNptzr9KxEAQBvDsKSgoCHYWga0s9CVUrvCKrMKClsfkMh6r+aOzEySlMO4DHAjXaF7Pt9BNlCvEbhi++c23"
            "veeZECruHrF4+dwSdSYiR8ZmLUNe4jkzubxlLGwc6mUicmjsP9tdY6/ye1xw8rqKwsFG+EmoMfE7v4k6TS+x"
            "00Con1F7dmWpm1rfNaS5KaALMnEzlQ7SvrFTt2DX1EBdEk/dZCXr9fF8/jDLLsDjLTmO6tQRjrFNOQMVhoGI"
            "n01b5Uix546xN1C2OEgnIuqp/wqDl45eht7DEq+B+C/z/tH3SQjhGzvRdKw="
        ),
        'Hey are we still on for today',
    ),
    "typedstream_unicode": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUVsnOTHt8EqFRzOmvt/Rr/CsY+KT3Wuezeh7umTli+WNCh/mT+z9ML9/"
            "r0Je4uH1ZaltLUyZnowSIIN5/IJdMpNLMvPzEosqGYAmZTJNapkxQy0+PtvT1ymxODW8KLMEaIlLZlEqWBnc"
            "rX6JualtICOADvErzU1KLQI6m90vOCwxpzQVZJJWSwtj4fz/bSDzZMHm+aYWFyempwYkFpWgGzN33vz5DG1t"
            "bQB3/4Tm"
        ),
        'café ☕️ 我们明天见 👍🏽 naïve',
    ),
    "typedstream_long": (
        _blob(
            "eNrtkkFKxDAUhptRUFAQ3IkIWblQ8Awqs5lFoxDQ5ZC0z5qZtplJX9S6G4g5gCDMRns9b6FJlVmIN9Dd438f"
            "3+pb32rQgKiwnUG+eF9z5NQ5t8d4alHIEs4QjZIWIefhqIvEuV3Gf1k3Gb+QE8gweXoOhp2V4YsgPfF9vzhy"
            "vNgf4C3QuVXZlEqj72t6ox/oxFazhuo7MDS+S/HY0lwXJ/Qf/kuwdwM1IqGR2NI240OVodK1MG0S4lFhXi4P"
            "x+PpKD0XDVwbhaGroTLQY6s8majAR0Voj9lKggmlbjB+JUoL0XTkHJl3Hz76DnpfCk0jCrgUBn9qXt+6LvHe"
            "fwJd8i6K"
        ),
        'the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog. the quick brown fox jumps over the lazy dog.',
    ),
    "typedstream_detected": (
        _blob(
            "eNptz89Kw0AQBvBsq1XqPxAqeFByVrEHb4Kg0EuhWW0XvZZNMsTYZFOTCbSIB2HcBwgIrZXq4/kYJq30oL0N"
            "wzc/vlnZSDAGGeKwD+7Ld5nYJRHtc2GlKO0ArhBj304RXJEPyjOIdrlYsl3n4tp+AAeN1ywXdhbCPMFmid/5"
            "jdjxWQCYmCEAmiJVrhyaEk0VRerETADMe8R+cl6vw0CG/QBOnSisDzSV/CarFv4mFw3fQT9SMh4aOeizjEaj"
            "g26317QsSBLpwY2McdGUyxB0cZnX4GloQ5yXXuPiTgYpFMAREXv8NLQelyoZvU9KhXc48xoSZQMwfw7cv+B0"
            "SrT11OWiI5UHF+3281e1orNJ9qHHbHtcri2svZnV8lXvf6lVLm47raKGYxTZ2vL/567+ATDnpaI="
        ),
        'lets meet Sunday at noon, see https://example.com/x',
    ),
    "typedstream_multirun": (
        _blob(
            "eNptjsFKw0AQhrNJQUFB8eZByMmDnnwDlV56yCos6LFskrGuNmndnRx6FMZ9gIBQrRofz7fQSSo9WE/z8/Hz"
            "zd/bcmhBFzibQv74FZE4JaJ9qZIKdTqGM0Rr0gohVxzKUUC0J9U/dFOqi/QOMgyeajbsrAzLhugav/mZxPHJ"
            "jbEO46m2GN+ChViXeewgm/DpIK6o5mxs7ik0A7Hb2rel6psMzaTUdhawzoQ1zeeHw+H9IDnXDq6tQf7UNxa6"
            "2mqw1AX4VsFrZFWkYHn7hlRXelxBazoiEg/Nt299B50vAef0CC551F/N+0fTBN6/hLzqdRHWi/qt/lxiwTha"
            "xyHjnljDkff+B13cnXY="
        ),
        'first part here and second part there and a third',
    ),
    "typedstream_multiline": (
        _blob(
            "eNptzr9qAkEQBvBbE1CMINgJClYW5iVUbCxuFRZiKXs6ysb7Y/ZmEUth3AcQAjbJvV7ewuydYiF2H8M3v5nX"
            "txQ1yAj3W1ge/l6I9YmoyYVvUAYhDBC1CgzCUrgQrz2iBhdPphUuJsEnLNA7npxQvwvXBisat/xN7L0Vqhg6"
            "SQzVIuAuqV7TKjHaUkmNWSuHalyM1AJVEku999ymKp3ofO7O55uxP5QpzLRCh46UhqJ2/43LCGxOuMPcRAFo"
            "92aZiw8ZGsilHhH7yi4299qF50OayjVMpcZH5uc3yzxr7T+m6XQR"
        ),
        'line one\nline two\n\nline four',
    ),
    "typedstream_empty": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIuQX7FhSUpSZVFqSmhIMZOSlM7S0cPgF+ydlpSaXMLROagFzIVKM"
            "U1oYtRna2gBAwRvd"
        ),
        None,
    ),
    "typedstream_attachment": (
        _blob(
            "eNptzs0KAVEUwPG5KIpSdha2FrwEsrGYS91iqTvmpIvxcefMwlId9wGUsmFeRHkdO4/AnSEL2Z1O//Pr5Ioh"
            "apABbtfg7+5ZYi0iqnLhRii9BbQRtfIiBF/YYTl1iCpc/NkWuOh7M5igsz9YofwV3gVLi898JNbMPm5XQxnV"
            "YyzpS1x01QTVain11rGByhzodKqPx/Oe25EhjLRCe9tVGtLs+wKXAZiEsD6PAg+0/SbPxVAuIkikBhHbxE+T"
            "eLXUcyEM5RQGUuMvc77EsWOMeQEfSGx5"
        ),
        None,
    ),
    "typedstream_mixed": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUVv0/f49Ofn52QqJJQolGZnFCgUZ+SX5bS1MmZ6MwiATePyCXTKTSzLz"
            "8xKLKhmAWjKZJrXMmKEWH5/t6euUWJwaXpRZAjTNJbMoFawM7ii/xNzUNpARQBv9SnOTUouA7mP3Cw5LzClN"
            "BZmk1dLCWDj/fxvIPFmweb6pxcWJ6akBiUUl6MbMnTd/PkNbWxsA54NzaQ=="
        ),
        'look at this photo',
    ),
    "typedstream_len126": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUbsucUBBWwtTpidjHcipPH7BLpnJJZn5eYlFlQxAt2UyTWqZMUMtPj7b"
            "09cpsTg1vCizBOhsl8yiVLAyuO/9EnNT20BGAL3mV5qblFoEDAh2v+CwxJzSVJBJWi0tjIXz/7eBzJMFm+eb"
            "WlycmJ4akFhUgm7M3Hnz5zO0tbUBAMv+msU="
        ),
        "a" * 126,
    ),
    "typedstream_len127": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUbs+cWBBWwtTpidjPcitPH7BLpnJJZn5eYlFlQxAx2UyTWqZMUMtPj7b"
            "09cpsTg1vCizBOhul8yiVLAyuPf9EnNT20BGAP3mV5qblFoEDAl2v+CwxJzSVJBJWi0tjIXz/7eBzJMFm+eb"
            "WlycmJ4akFhUgm7M3Hnz5zO0tbUBAGX8myg="
        ),
        "a" * 127,
    ),
    "typedstream_len128": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUbuxgSFxgEFbC1OmJyPQISAH8/gFu2Qml2Tm5yUWVTIAXZjJNKllxgy1"
            "+PhsT1+nxOLU8KLMEqDjXTKLUsHK4GHgl5ib2gYyAuhBv9LcpNQiYHCw+wWHJeaUpoJM0mppYSyc/78NZJ4s"
            "2Dzf1OLixPTUgMSiEnRj5s6bP5+hra0NAPe9nI0="
        ),
        "a" * 128,
    ),
    "typedstream_len255": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUbvxP0PiyAZtLUyZnozAcACFF49fsEtmcklmfl5iUSUDMIAymSa1zJih"
            "Fh+f7enrlFicGl6UWQIMO5fMolSwMngU+CXmpraBjACGr19pblJqETA22P2CwxJzSlNBJmm1tDAWzv/fBjJP"
            "Fmyeb2pxcWJ6akBiUQm6MXPnzZ/P0NbWBgB1Ks2q"
        ),
        "a" * 255,
    ),
    "typedstream_len256": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUbuRgTFxhIO2FqZMT0ZgQIACjMcv2CUzuSQzPy+xqJIBGEKZTJNaZsxQ"
            "i4/P9vR1SixODS/KLAEGnktmUSpYGTwO/BJzU9tARgAD2K80Nym1CBgd7H7BYYk5pakgk7RaWhgL5/9vA5kn"
            "CzbPN7W4ODE9NSCxqATdmLnz5s9naGtrAwAPZ8wP"
        ),
        "a" * 256,
    ),
    "typedstream_len32767": (
        _blob(
            "eNrtzLFqFFEYhuGZRDCQgGBnYWthbsJImhQ7CgNahjPJYTkxu+rsmSJVWDjOBSwE0ujennexZjeSQryE56l+"
            "fj7eZ4eL3Mcwyzff4uXy936p35VSXjXtZMihu44nOfepG3K8bB+O+bQq5WXT/ud70LQfuqt4kasfq4fCi6fC"
            "46LeLf7ed6U+Xm5uAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwlr10Vi83t6tSylHTnqaLnL7O"
            "Q39T3ZU67a3K/f2b8/MvZ5P3YRE/9ymn+fQ09XE3O8m5T92QYxNmcdwmDpq2GWZd7KtSnjftp3A9xG3pbSn1"
            "9/Vm3PZe73qTuFiEafwY+vxv5uev9boax/EPF8/weA=="
        ),
        "a" * 32767,
    ),
    "typedstream_len32768": (
        _blob(
            "eNrtzLFqFFEYhuEzUTAQQbCzsLXQmzCSJsWOwoCW4UxyWI5mV509U6SL4TgXsBBIo3t73oXJrmIhuYTnqX5+"
            "Pt6HB6sypLgoF1/S2bdfD2rzutb6rO1mY4n9eTosZcj9WNJZd3cs56HWp213z3e/7d72H9NpCd/Xd4Un/wp/"
            "Fs1u8fe+rs2rq3AZQgQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4lT38nFzFS5DWNdaH7fdUT4t"
            "+fMyDhfhujZ5b11vbl6cnHw6nr2Jq/RhyCUv50d5SLvZYSlD7seS2rhI0zax33btuOjTEGp91Hbv4/mYtqWX"
            "tTZfN7+nbe/5rjdLq1Wcp3dxKP9nfvzcbMI0TbcPFO7f"
        ),
        "a" * 32768,
    ),
    "typedstream_len65535": (
        _blob(
            "eNrtzLFqFFEYhuEziWAggmBnYWuhN5FImhQ7CgNahjPJYZkku9HZM0VKw3EuYCGQRvf2vIsxu4qFeAnPU/38"
            "fLxPDle5T3GRbz+ni68/90t1VEp5WTezIcf2Oh3n3HftkNNF83gs56GUF3Xzn+9B3bxvL9N5Dt/Wj4Xnfwu/"
            "F9Vu8ee+L9Xbu2kKIQIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAECMY9nrTqu7aQphXUp5Vjcn3Xnubpaxvw33per21uXh4fXZ2dXp7F1cpU99l7vl/KTr"
            "0252nHPftUNOdVykcZs4qJt6WLSpD6U8rZuP8XpI29KbUqovm2nc9l7terO0WsV5+hD7/G/m+4/NJozj+Aum"
            "+3RZ"
        ),
        "a" * 65535,
    ),
    "typedstream_len65536": (
        _blob(
            "eNrtzLFqFFEYhuEzUTCgINhZ2FroTRhJk2JHYUDLcCY5LEezq86eKVIajnMBC4E0ureXu4i7G7EQL+F5qp+f"
            "j/fh41UZUlyUy6/p/Pvtg9q8qbU+b7vZWGJ/kY5KGXI/lnTebY/lPNT6rO3+8z1su3f9p3RWwo/1tvD0b+F+"
            "0ewXf+7r2ry+CqEJEQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAALamepBPmqsQmrCutT5pu+N8VvKXZRwuw3Vt8sG63ty8PD39fDJ7G1fp45BLXs6P85D2"
            "s6NShtyPJbVxkaZd4rDt2nHRpyHU+qjtPsSLMe1Kr2ptvm3upl3vxb43S6tVnKf3cSj/Zn7+2mzCNE2/AYoK"
            "cMA="
        ),
        "a" * 65536,
    ),
    "typedstream_selfref": (
        _blob(
            "eNptzsFKw0AQBuBsFRQUBG8ehPTSg76ELb30kK2woMcyaYZ2tWnj7ATpsTDdBygUetG8nm/RbqIUEW/D8M83"
            "/+mFY0LIeVlgtvo6EfUgIjfaJCVDOsMuM9m0ZMxMGOaTSORam3+259oM0xccc7TeBOHqKHwnVJP4mbei7ts8"
            "xfh9QVn8q0AMRYFALp4ioZeWHah2rV1q07djtos50DIK57a1kd2uMxq9DpIeOHwmy0HuW8ImdiyoIUdfE+G7"
            "LvMUKXQ90+YJZiXW0p2Ieqv2vvZuGy9B52CCj0D8l/n4rKrIe38AoXt3DA=="
        ),
        'the word streamtyped appears here',
    ),
    "typedstream_classref": (
        _blob(
            "eNptzr9KA0EQBvDbKCgoCHYWwhVi4b2ESpoUtwoLWobZuyGsuT+6O1tcKYz7AAEhjd7r5S3i3hlSiN18w8eP"
            "7/DEkUWoqXvF8n1zwOKWmS+kyj2BrvCOyBrtCUsVj2aRMJ9L9c/3WKoH/YIFJR+rKJzthd+GGBu7+5NFlhFU"
            "yxhS0K2n9KqowLkGakyhKXcRXUptCV3giZmJbHBPpZqagkzbgO2SCJnJitfr6/l8OcvvweGzNRTZqbE41vZT"
            "ZbTDQMQd0tcabVx9JNUTVB4H6YZZvPXbMHiXo5ejc7DAR7D0l/n67vskhPAD3456Rw=="
        ),
        'talking about $classname and $classes today',
    ),
    "typedstream_rtl": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUVvyZuuNjTfW3lhxY7kCiLjZcmMniLzZ2tbClOnJyAsyhccv2CUzuSQz"
            "Py+xqJIBqC2TaVLLjBlq8fHZnr5OicWp4UWZJUATXTKLUsHK4A7zS8xNbQMZAbTVrzQ3KbUI6EZ2v+CwxJzS"
            "VJBJWi0tjIXz/7eBzJMFm+ebWlycmJ4akFhUgm7M3Hnz5zO0tbUBADZge+M="
        ),
        'مرحبا بالعالم',
    ),
    "typedstream_plainstring": (
        _blob(
            "eNpj4S4uKUpNzC2pLEhNaXzB3MLo0NLSIukX7FtakpiUk+pYUlKUmVRakpoSDGTkpTO0tAj5BWMR5fAL9k/K"
            "Sk0uYWidBDSBH24CRAUjWAWUPbWFUVs4q7S4RCFRoSAnMTNPoRgs0dbClOnJKAzSz+MX7JKZXJKZn5dYVMkA"
            "1JDJNKllxgy1+PhsT1+nxOLU8KLMEqAWl8yiVLAyuJP8EnNT20BGAO3zK81NSi0Cuo7dLzgsMac0FWSSVksL"
            "Y+H8/20g82TB5vmmFhcnpqcGJBaVoBszd978+QxtbW0AKnFxYw=="
        ),
        'just a plain string',
    ),
    "keyed_plain": (
        _blob(
            "eNp9UEtPFEEQrlpeC+oyLPhgcXGE4bEgsKCJZwwHiO4gmRUWQjLp3W1wZJwh3b2YSTT0wZgYY4wJFy+GrEfO"
            "HLhx4a48Dv4FPXj1hp3ZyUYufJeqrvq66vuquOk6XGSzpxhraGxqbmkrGFuUccf3lg3CSs8d9cobwt8sGH7x"
            "BS0JngR8t2tr7ab1mAa0PB2RfsRb88z3hcS9K1c7b/YOZ0ZG7008M7yK655cS7RrHcmCaVmCOd76olFyCeer"
            "pjUtVKFYEZTLmGyTjceJrus3lk1rnIdE2WBr6Vka6IRR/RXVuXBcV/c9fc1nuvDLJDi+1Z3qWanN88hLWqil"
            "lIcKcxVBii6tbf2Wup2uS1DJfOjn5I6euGsMLamdGzTgKypGRqt9/bJJNlcHBmWLjMtWWxu07Y253CPC6RJz"
            "hJoy4zDFVMeq+zCVBqU5JOYo52SdPiVMXOh3nkfQQMkfG1d3mHHCMYQF1bG0KmYnba27rr/+uxw5mbqfvqxv"
            "a8n/ThsVIQ4dkAIDMjAFD2EOnsACWGADBRcq8Brewwf4CJ9gB77CLhzCEXyHn/Ab/sBfRIyjhj3Yizr24QAO"
            "YQZHMY8C3+A2vsXPuINfcB8P8AxCxLAW4QFcAP76B4ANwtc="
        ),
        'Hey are we still on for today',
    ),
    "keyed_unicode": (
        _blob(
            "eNp9kE9rE1EUxc9N/6WtJpO01TY1Gu0YjdqYtooiVah0UzSjMtUmRRhm0tc4dpwp8yaFgItZKUVEEcGdlCi4"
            "0C/RXRdubFxkIbhwo1DwE7T6mIRgN97Nfe/e8+77nWusWib3crmvFOro7Oru6SvIa8zlpmMXZd0tPTDFbV72"
            "nNWC7BgPWcnjcdCTDU2KKuoNVmVLMy3Rl3DvvOs4nk8f+w8MHD56OnPm7Lnzd2W7Yln1g5GoFIsXFFX1XNMu"
            "35NLls75fUWd8UTBqHiM+yG/z+/cjgwOHSoqapYHQr/DkYZRgo5l/EIqPbQXRcqIKTvLkWJmfROpxtVv+cb0"
            "912kYAvVb6yBbQ+PJEYXm1/Y+iNWaB4ZD6DzFU83LNYEeZc4kmxTicOtwGL9WCpyXD61IDBWWJUvitzyXjsx"
            "5nf53bWTab/HD/u9mpTWtJW5/HWdswXX9MSUWdMVSrG/tjVFMGhSMhDmGed6md3WXW9ff+BPKyQI/PGsWM2s"
            "GYzR3WptPCmKuQlNGmnzt18vtZxMTiX/19ek+D/bbhURRgwJyMhgEpcwh5u4AxUaGCxU8BjreIbneIHXeIsN"
            "1NHAD+xgj0LUT4M0SmOUpQmaoot0ma7QNF2jMj2ll/SK3tB7+kCfaIs+008EEaJmxgXsC9r9C9V6yEY="
        ),
        'café ☕️ 我们明天见 👍🏽 naïve',
    ),
    "keyed_detected": (
        _blob(
            "eNp9kk1sE0cUx+e9DSYfkExCHD4K2NBtCq7jJKQV/Ug/XFJIQmxINjFOIGzH9uAsXu+6u+MIq6o0J6qq4lD1"
            "wrE4ilRVVUEUVUUI9VAhDj01yaFHrqiX3iuhjtdpSiq1c5k3M/v+7/f+b3MV2/LF0NAGoNayI7SzPasvc8+3"
            "XGdeZ15+yVKnWV24lazu5q7yvPB7CFz/yqRdaeMsr/FCcvOjX1vbZj3XFRJu030du8L7oy8nBodG3hod++D0"
            "xPRMdv7Sh4WlOd2p2vbG7s4u2t2zpzebNgzhWU4xkEsKFeeqgk84V9xLz539jJ63me9LlGHZIveu0b69++bT"
            "RsIPkqVm0hGbCz9a5lxEjapTYLUoE1HHdZ141Oc8uiRExX9zcJBfY+WKzRN5tzx4be3ACwcPLTSlHVbm2WbI"
            "/YAmVRUsZ/Mm38rBw5EtWBWcC6xYO0KP9i8ojk1nVl7UX5I7ZJvskr3rx47Q468MXFCvJV7zb8Vk6FZc7pSt"
            "Jj1kmqWJVIr7Pivy88wTW42mFQUlimv4hOp/zMoLNQfm1erDkYbeqycH6q/FZLsM1V+Py46m2uFAbYwJNsaF"
            "olATeV5u4+133qXvJd8/ZdIexeIxp8iXmZ2wuVMUSybt3Xbp5lmjZKMnv8LzFrPlLrmbtshOGqLtCuzMuOoo"
            "w+wqr58JmCZTA/WzMUkVUzouu5tMfQHTlOWUtrGsG3R2LnOh4UmO+fxiozS3VcVlLoncI3tMGv6vSS1cnEsb"
            "czNT9YWIOi1evrw1oaTnsdrKohlRukG8xmgu3yhSUKaMt5MQdJIwyj6Vx68sbuU1LFvhxUhGOa1C9WpdNen+"
            "f3T/Ri9s/gMlO/J/74HB/74kraSbHCA6OU5OkJNkgkyRaWIQhwjyCblObpJV8i35jtwhd8k9cp/8TB6RP4FA"
            "B/RCP8RgBEYhCVNwHmZgFjKQBROKYEEJyuDCR/AFfAk3YRW+httwF+7BD3AfHsBD+AmewFN4hmGM4lHUsR+P"
            "YQzjOIxv4CiewtM4jpOYwnM4jQba6OPH+Cl+hp/jDVzFb/AOfo8/4mP8BdfxN3yCT/F3/AOfaai1aZ1aTEto"
            "kyRYCM2dlMm2pZl/AVOHVH0="
        ),
        'lets meet Sunday at noon, see https://example.com/x',
    ),
    "keyed_multiline": (
        _blob(
            "eNp9UE1PE1EUPbd8tFRth+IXlRHQsXwoWNHENYYN0Y6YASnEZDItTxgZZ8i8N5omxLzExMQYo4kuXBlSl25d"
            "uHPj3ggs/Au6cOtSXmYmjWy8m3vevefde85tbHkuF9XqPmW6unt6s/m68YiF3A38FcMJmxuuei0aItiqG0Hj"
            "AWsKXgI927G1omndZC22NpuSvuf6FsMgEJI+Hjk6cOrs+MTkxUuXlww/8ry9Y4Wi1l+qm5YlQtdfv2s0PYfz"
            "e6Y1K1ShEQnGZUbmZfdu4fiJkyumNc1jouyytSHP9dlI4LN8DMTjIJ+g+0EU7p4eLJ9ZTcb5zkNWTyDjscBa"
            "JJyGx5KlH8pDekeBArdjO3vDI4VRY2xZrdxkLb6qcuqzfe687JG97QsVmZU52WdrFdvenK/dcDhbDl2hpsy5"
            "oWKqW3VsmEqDrekxscY4d9bZghOKQ/2Bv2loUPKnptUZ5tx4jBO22lO6Klav2NpgR3/n91rqZOaq/r++rZX+"
            "uWxaRA79KMPABGZwHfO4hTuwYIPBQ4RtPMcLvMQrvMV77OALvuIbfuAXfuMPgbJUpDLpNEyjZFCFxmmSLOK0"
            "TU/oKb2mN/SOPtFn2kccGUoyruFQ0M8DNojCHw=="
        ),
        'line one\nline two\n\nline four',
    ),
    "keyed_len65536": (
        _blob(
            "eNrtz01P1EAAh/GW1wWEXcA30PW1oqggoomJNwwXolRNURZC0nSXCRbWXdJ2NRgTezIxxnjxZqIEEy/cvXjw"
            "Kwjo1as3j171n4UQ9eAneH7Jk7Yz0+m0uFwO42Rk5Ivd0NjU3NLaXnAemCgOq5UZJ4hK90I9TTlJdbngVIuL"
            "ppTEPZb9dNXPZV3vulkx82M7izYybVNRtZqk9nrHnt4DR84Mnj13/sIdp1Irl7c6u7K57p6C63lJFFYW7jql"
            "chDHc643lmigWEtMnDak7WnTZtfefftnXG84ri9MG319zrICAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAABk82Bf/6FZp1QO4rgS3DeF7VsT+7ms603WkqBYNl4ShZWFd/2H8wXX237Qzc3ioiklW0ePdR13Tk+7"
            "3vCSWYlnda3WJ+K1EyfT5rRl7dRA2ppm0jY/N+D7SxOT14LYTEdhol3Gw0grw2plLNGuxVpiXJ3Bz+XrCydN"
            "HAcL5lYQJX/N9/7akbN0/KHhOdcbD+vbBNHK2lBegyMX/Vzf7vl3357f+ZPRS/n/zfu5Htf7d9CyrIzqVv3K"
            "UYNqVF1RE+qGuq085SujyqqmHqtn6rl6oV6qV+qNWrVs671aVx/UJ7Whvqpv6rv6oX5atm2rJtWiMqpDdaqs"
            "6lZX1awqKaMW1UP1SD1Rr9Vb9dGqa7K3r9Zl60+2/fk3yly+Fg=="
        ),
        "a" * 65536,
    ),
    "keyed_tapback": (
        _blob(
            "eNp9UE1rE1EUPTdN27TadJpqNbGjo06rVVtjqxRExEo3RTMqU21ahGGSPOKY6UyZ9yIEXLyVUEQE6caNlAiC"
            "CP4QF4Ktbty7ceFfqK+TIdiNd3O/zrvvnFPZ8D0uisXvlOpJ9/b1D5bNZyziXhismm5UfeKpbtkU4UbZDCtP"
            "WVXwHOjFtqMNW/Yd1mK1hQT0LTOwHIWhkPTp0OHRYyfPT124eOnyQzNo+v7uUHZYG8mVLdsWkRfUH5lV3+X8"
            "sWUvCDWoNAXjMiUHZXone+To2Kplz/AYKHtCLY+78NAAQw2GMQ6OEE0EquMwUFdduL+BUNnAOpih7xzPF06s"
            "dT4J3HVW7pSMx7RLTeFWfNah8r4wrnd5qeJeLHL3lJE9bZ5bUUQarMXXVE7Ut8+clb2yrz0xKftlRg442qTj"
            "NJZKt13OViJPqCuLXqSQysGuOEtxcDQ9BpYY526d3XcjcWA/upeEBkV/ekaZs+jFZ9yo1Z7e11S84mj5Lv/u"
            "61qiZHZO/9/e0XL/+J0MkcEICjAxhVnMY0l5/QA2HOW2r1x+jk28xCu8xhbeYRs/8BO/8IdAaRqiMdJpgoo0"
            "R9donq7TDbpJt8ijTXpDW/SWPtBH+kxf6Cv9Rhwp6mRcxYGgvb8g1MDR"
        ),
        'Liked “sounds good to me”',
    ),
    "keyed_plainstring": (
        _blob(
            "eNp9UDFv01AQ/i5t07RA6iSltCmmKZhCgZZQkJiLuhSIAbnQtEKyXtKn4tTYkd8zUhCDJxAg1IUZVWHswMbC"
            "zsKAaPkBMLCysgFPthXRhVvu3nffu/u+a7RdR8hq9Stl+voHsoPDdeMRD4Tje2sGC5oPHPVaMaTfrht+o8Wb"
            "UhRBT3dsbcS0bvAO31hMSV9yQyuB78uIdg8dLh07cXb23PkLF+8aXui6+0fyI1qhWDctSwaOt3nPaLpMiPum"
            "tSgV0AglF1EmGo769/KjR8fWTGtexMSoz9ZKrVDICqu0XeZ4lQTfG58oT64nUzz2kNeTkotYVy2UrOHyZNfb"
            "8nG9t1gVt2IX+1OV/LRxZlVt2uIdsa5yaq978lQ0EGW7p2eiwSgXDdnajG1vLdeuMcFXA0eqKUtOoJjqRD31"
            "ptJga3pMrHEh2Ca/zQJ5oF/6k4YGJX9uXrlfcuIxLOh053QFVi/Z2kRPf+/3Rupk4bL+v76tFf85aAoihwLK"
            "MDCLBVzFMm7iDizY4HAR4gme4yVeYRuv8QY7eI8P+IjP+I4f+Ilf+E1ZKtAojdE4TZJOUzRN16lFgkJ6TM/o"
            "BW3TLr2jT4gjQ0nGFRwI+vYX+GzAag=="
        ),
        'just a plain string',
    ),
}
