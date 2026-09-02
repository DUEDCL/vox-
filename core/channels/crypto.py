"""AES-128-ECB，纯 Python。**为了不引依赖，而不是为了造轮子。**

微信 CDN 的媒体协议要求 AES-128-ECB + PKCS#7（Hermes 用的是 `cryptography`）。这台机器的
虚拟环境里没有 `cryptography`、没有 `aiohttp`、没有 `requests` —— 而为了「发一条语音」
装一个带 C 扩展的加密库，代价比这一百来行大：它要编译、要跟着 Python 版本走，而这里需要
的只是一个分组密码的教科书实现。

**ECB 在这里不是密码学选择，是协议要求。** 一般情况下 ECB 不该用（同样的明文块出同样的
密文块），但这条链路上的密钥是**每个文件一把、随机生成、随消息一起交给对端**的
（`aes_key = secrets.token_bytes(16)`），所以它的作用是「让 CDN 上那份字节不可读」，
不是「长期保密」。换成 CBC 会直接违反协议 —— 对端按 ECB 解。

**S-box 是算出来的，不是抄下来的。** 256 个十六进制数抄错一个，症状是「对端说文件损坏」
而这一层每个断言都是绿的。用 GF(2^8) 的乘法逆元 + 仿射变换现算，错就在第一条 FIPS-197
向量上炸 —— `tests/test_channel_crypto.py` 钉的正是那几条官方向量。
"""

from __future__ import annotations

BLOCK = 16


def _xtime(value: int) -> int:
    """GF(2^8) 上乘 2，模 x^8 + x^4 + x^3 + x + 1（0x11B）。"""
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _mul(a: int, b: int) -> int:
    """GF(2^8) 上的乘法。"""
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _build_sbox() -> tuple[bytes, bytes]:
    """S-box 与它的逆，现算。

    每个字节先取 GF(2^8) 里的乘法逆元（0 的逆定义为 0），再过仿射变换
    ``s = inv ^ rotl(inv,1) ^ rotl(inv,2) ^ rotl(inv,3) ^ rotl(inv,4) ^ 0x63``。
    """
    inverse = [0] * 256
    for candidate in range(1, 256):
        for probe in range(1, 256):
            if _mul(candidate, probe) == 1:
                inverse[candidate] = probe
                break
    sbox = bytearray(256)
    for index in range(256):
        inv = inverse[index]
        value = inv
        for shift in (1, 2, 3, 4):
            value ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        sbox[index] = value ^ 0x63
    inv_sbox = bytearray(256)
    for index, value in enumerate(sbox):
        inv_sbox[value] = index
    return bytes(sbox), bytes(inv_sbox)


SBOX, INV_SBOX = _build_sbox()


def _rcon(round_index: int) -> int:
    """轮常量 ``x^(i-1)``，同样是算的。"""
    value = 1
    for _ in range(round_index - 1):
        value = _xtime(value)
    return value


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 的 11 个轮密钥，每个 16 字节。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 要 16 字节密钥，给的是 {len(key)}")
    words = [list(key[index * 4 : index * 4 + 4]) for index in range(4)]
    for index in range(4, 44):
        previous = list(words[index - 1])
        if index % 4 == 0:
            previous = previous[1:] + previous[:1]  # RotWord
            previous = [SBOX[byte] for byte in previous]  # SubWord
            previous[0] ^= _rcon(index // 4)
        words.append([a ^ b for a, b in zip(words[index - 4], previous)])
    return [
        [byte for word in words[round_index * 4 : round_index * 4 + 4] for byte in word]
        for round_index in range(11)
    ]


def _add_round_key(state: list[int], round_key: list[int]) -> None:
    for index in range(16):
        state[index] ^= round_key[index]


def _shift_rows(state: list[int]) -> list[int]:
    """列主序（state[r + 4c]）下的行移位。"""
    out = list(state)
    for row in range(1, 4):
        for column in range(4):
            out[row + 4 * column] = state[row + 4 * ((column + row) % 4)]
    return out


def _inv_shift_rows(state: list[int]) -> list[int]:
    out = list(state)
    for row in range(1, 4):
        for column in range(4):
            out[row + 4 * ((column + row) % 4)] = state[row + 4 * column]
    return out


def _mix_columns(state: list[int], inverse: bool = False) -> list[int]:
    matrix = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    out = [0] * 16
    for column in range(4):
        col = state[4 * column : 4 * column + 4]
        for row in range(4):
            out[row + 4 * column] = (
                _mul(col[0], matrix[(0 - row) % 4])
                ^ _mul(col[1], matrix[(1 - row) % 4])
                ^ _mul(col[2], matrix[(2 - row) % 4])
                ^ _mul(col[3], matrix[(3 - row) % 4])
            )
    return out


def encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[0])
    for round_index in range(1, 10):
        state = [SBOX[byte] for byte in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        _add_round_key(state, round_keys[round_index])
    state = [SBOX[byte] for byte in state]
    state = _shift_rows(state)
    _add_round_key(state, round_keys[10])
    return bytes(state)


def decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[10])
    for round_index in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [INV_SBOX[byte] for byte in state]
        _add_round_key(state, round_keys[round_index])
        state = _mix_columns(state, inverse=True)
    state = _inv_shift_rows(state)
    state = [INV_SBOX[byte] for byte in state]
    _add_round_key(state, round_keys[0])
    return bytes(state)


def pkcs7_pad(data: bytes, block_size: int = BLOCK) -> bytes:
    """**永远补一整块**（数据正好对齐时也补），这是 PKCS#7 的定义。

    少了这一条，长度刚好是 16 倍数的文件解出来会被砍掉最后 16 字节 —— 而那是个只在
    「文件大小恰好对齐」时才出现的故障，最难复现的那一类。
    """
    padding = block_size - (len(data) % block_size)
    return data + bytes([padding]) * padding


def pkcs7_unpad(data: bytes, block_size: int = BLOCK) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("不是整块的密文")
    padding = data[-1]
    if not 1 <= padding <= block_size or data[-padding:] != bytes([padding]) * padding:
        raise ValueError("PKCS#7 填充不合法")
    return data[:-padding]


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """加密。**自动补齐** —— 和 Hermes 的 `_aes128_ecb_encrypt` 同一个形状。"""
    round_keys = _expand_key(key)
    padded = pkcs7_pad(plaintext)
    return b"".join(
        encrypt_block(padded[offset : offset + BLOCK], round_keys)
        for offset in range(0, len(padded), BLOCK)
    )


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """解密并去掉填充。

    填充不合法时**不抛**：微信 CDN 上的某些文件是不带填充的原始长度（下载路径按
    `rawsize` 截断），所以这里把「解不出填充」当成「就是这一段字节」，由调用方按
    `rawsize` 裁。抛异常会让一条能播的语音变成一次失败。
    """
    round_keys = _expand_key(key)
    if len(ciphertext) % BLOCK:
        raise ValueError("密文长度不是 16 的倍数")
    plain = b"".join(
        decrypt_block(ciphertext[offset : offset + BLOCK], round_keys)
        for offset in range(0, len(ciphertext), BLOCK)
    )
    try:
        return pkcs7_unpad(plain)
    except ValueError:
        return plain


def padded_size(size: int, block_size: int = BLOCK) -> int:
    """PKCS#7 之后的长度。iLink 的 `getUploadUrl` 要这个数（`filesize`）。"""
    return size + (block_size - (size % block_size))


__all__ = [
    "BLOCK",
    "aes128_ecb_decrypt",
    "aes128_ecb_encrypt",
    "padded_size",
    "pkcs7_pad",
    "pkcs7_unpad",
]
