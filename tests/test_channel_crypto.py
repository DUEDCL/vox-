"""AES-128-ECB 的正确性，钉在 FIPS-197 的官方向量上。

## 为什么这个文件必须存在

这台机器的虚拟环境里没有 `cryptography`，所以那一百来行是我们自己写的。一个自己实现的
分组密码只有两种状态：**被官方向量钉住**，或者**看起来能跑**。后者的失败形状是「对端说
文件损坏」，而这一层的每个断言都是绿的 —— 加密的错永远不在本地暴露。

S-box 是**算出来的**（GF(2^8) 乘法逆元 + 仿射变换）而不是抄 256 个十六进制数，抄错一个
的症状同上。算错的话第一条向量就炸。

Evidence level: AUTO（官方已知答案，零网络）。
"""

from __future__ import annotations

import os

import pytest

from core.channels.crypto import (
    BLOCK,
    _expand_key,
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    decrypt_block,
    encrypt_block,
    padded_size,
    pkcs7_pad,
    pkcs7_unpad,
)

#: FIPS-197 附录 C.1 与附录 B 的 AES-128 已知答案。
VECTORS = (
    (
        "000102030405060708090a0b0c0d0e0f",
        "00112233445566778899aabbccddeeff",
        "69c4e0d86a7b0430d8cdb78070b4c55a",
    ),
    (
        "2b7e151628aed2a6abf7158809cf4f3c",
        "3243f6a8885a308d313198a2e0370734",
        "3925841d02dc09fbdc118597196a0b32",
    ),
)


@pytest.mark.parametrize("key_hex, plain_hex, cipher_hex", VECTORS)
def test_one_block_matches_the_official_answer(key_hex, plain_hex, cipher_hex):
    round_keys = _expand_key(bytes.fromhex(key_hex))

    assert encrypt_block(bytes.fromhex(plain_hex), round_keys).hex() == cipher_hex


@pytest.mark.parametrize("key_hex, plain_hex, cipher_hex", VECTORS)
def test_decryption_is_the_inverse(key_hex, plain_hex, cipher_hex):
    round_keys = _expand_key(bytes.fromhex(key_hex))

    assert decrypt_block(bytes.fromhex(cipher_hex), round_keys).hex() == plain_hex


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 255, 4096])
def test_round_trip_at_every_alignment(size):
    """16 的倍数是最容易错的那几个长度：PKCS#7 要求**正好对齐时也补一整块**。"""
    data = os.urandom(size)
    key = os.urandom(16)

    encrypted = aes128_ecb_encrypt(data, key)

    assert len(encrypted) == padded_size(size)
    assert len(encrypted) % BLOCK == 0
    assert aes128_ecb_decrypt(encrypted, key) == data


def test_padding_always_adds_a_whole_block_when_aligned():
    """少了这一条，长度刚好是 16 倍数的文件解出来会被砍掉最后 16 字节 ——
    而那是个只在「文件大小恰好对齐」时才出现的故障，最难复现的那一类。"""
    padded = pkcs7_pad(b"0123456789abcdef")

    assert len(padded) == 32
    assert padded[-16:] == bytes([16]) * 16
    assert pkcs7_unpad(padded) == b"0123456789abcdef"


def test_a_bad_pad_does_not_lose_the_bytes():
    """微信 CDN 上有些文件是按 `rawsize` 截断的原始长度、不带填充。解不出填充时**返回
    原样字节**由调用方去裁 —— 抛异常会让一条能播的语音变成一次失败。"""
    key = os.urandom(16)
    round_keys = _expand_key(key)
    # 明文最后一个字节是 0x7f，不是合法的 PKCS#7 填充。
    plain = bytes(15) + b"\x7f"
    cipher = encrypt_block(plain, round_keys)

    assert aes128_ecb_decrypt(cipher, key) == plain


def test_a_wrong_key_length_is_refused():
    with pytest.raises(ValueError):
        _expand_key(b"too-short")


def test_a_truncated_ciphertext_is_refused():
    """长度不是 16 的倍数说明这段字节在路上被截了。悄悄补齐它会解出一段垃圾。"""
    with pytest.raises(ValueError):
        aes128_ecb_decrypt(b"not-a-whole-block", os.urandom(16))
