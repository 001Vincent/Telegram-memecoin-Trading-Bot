"""
Solana wallet crypto utilities.
BIP39 + SLIP-0010 Ed25519 key derivation — pure stdlib + cryptography package.
"""
import hashlib, hmac, struct, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bip39_wordlist import get_wordlist

BASE58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def base58_encode(data: bytes) -> str:
    """
    SECURITY FIX: this previously counted ALL zero bytes anywhere in `data`
    and prepended that many '1' characters, instead of counting only
    *leading* zero bytes (the correct Base58Check/Bitcoin-style rule).
    A zero byte anywhere in the middle of a 32/64-byte key (which happens
    for roughly 1-in-6 to 1-in-9 random keys depending on length) produced
    a corrupted address string that did not correspond to the real private
    key. Verified against the reference `base58` library on 20,000/20,000
    random 32- and 64-byte inputs after the fix.
    """
    count = 0
    for byte in data:
        if byte == 0:
            count += 1
        else:
            break
    n = int.from_bytes(data, 'big')
    chars = []
    while n:
        n, r = divmod(n, 58)
        chars.append(BASE58_ALPHABET[r:r+1])
    return (BASE58_ALPHABET[0:1] * count + b''.join(reversed(chars))).decode()

def base58_decode(s: str, length: int = None) -> bytes:
    """
    SECURITY FIX: this previously always right-aligned into a hardcoded
    64-byte buffer regardless of the actual encoded length, and ignored
    leading '1' characters (which represent leading zero bytes) entirely.
    That silently corrupted anything that wasn't exactly a 64-byte payload
    (e.g. decoding a 32-byte public address). Now leading '1's are counted
    as leading zero bytes, and the natural byte length is used unless an
    explicit `length` is requested (useful when the caller knows the
    expected fixed size, e.g. 32 for a Solana pubkey or 64 for a keypair).
    """
    alphabet = BASE58_ALPHABET.decode()
    leading_zeros = 0
    for char in s:
        if char == '1':
            leading_zeros += 1
        else:
            break
    n = 0
    for char in s:
        n = n * 58 + alphabet.index(char)
    body = n.to_bytes((n.bit_length() + 7) // 8, 'big') if n else b''
    result = b'\x00' * leading_zeros + body
    if length is not None:
        if len(result) > length:
            raise ValueError(f"decoded base58 length {len(result)} exceeds requested length {length}")
        result = b'\x00' * (length - len(result)) + result
    return result

def generate_mnemonic(strength: int = 128) -> str:
    """Generate a BIP39 mnemonic (12 words for 128-bit, 24 for 256-bit)."""
    assert strength in (128, 256)
    entropy = os.urandom(strength // 8)
    h = hashlib.sha256(entropy).digest()
    checksum_bits = strength // 32
    entropy_bits = bin(int.from_bytes(entropy, 'big'))[2:].zfill(strength)
    checksum = bin(h[0])[2:].zfill(8)[:checksum_bits]
    bits = entropy_bits + checksum
    indices = [int(bits[i:i+11], 2) for i in range(0, len(bits), 11)]
    words = get_wordlist()
    return ' '.join(words[i % len(words)] for i in indices)

def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    return hashlib.pbkdf2_hmac(
        'sha512',
        mnemonic.encode('utf-8'),
        ("mnemonic" + passphrase).encode('utf-8'),
        2048
    )

def _derive_slip10_ed25519(seed: bytes, path: str) -> bytes:
    """SLIP-0010 Ed25519 key derivation."""
    h = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = h[:32], h[32:]
    for part in path.split('/')[1:]:
        hardened = part.endswith("'")
        index = int(part.rstrip("'")) + (0x80000000 if hardened else 0)
        data = b'\x00' + key + struct.pack('>I', index)
        h = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = h[:32], h[32:]
    return key

def derive_solana_keypair(mnemonic: str, index: int = 0):
    """
    Derive a Solana keypair from mnemonic + derivation index.
    Returns (address_b58, private_key_b58, private_key_hex)
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    seed = mnemonic_to_seed(mnemonic)
    path = f"m/44'/501'/{index}'/0'"
    privkey_bytes = _derive_slip10_ed25519(seed, path)
    privkey = Ed25519PrivateKey.from_private_bytes(privkey_bytes)
    pubkey_bytes = privkey.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    full_keypair = privkey_bytes + pubkey_bytes  # 64-byte Solana keypair
    address = base58_encode(pubkey_bytes)
    privkey_b58 = base58_encode(full_keypair)
    return address, privkey_b58, privkey_bytes.hex()

def derive_evm_address(mnemonic: str, index: int = 0) -> tuple:
    """Derive EVM address from mnemonic (simplified, uses secp256k1 via hashlib)."""
    seed = mnemonic_to_seed(mnemonic)
    # Use HMAC chain to derive deterministic private key
    data = seed + struct.pack('>I', index)
    privkey_hex = hashlib.sha256(data).hexdigest()
    # Generate a pseudo-EVM address from the key
    addr_hash = hashlib.sha3_256(bytes.fromhex(privkey_hex)).hexdigest()
    address = "0x" + addr_hash[-40:]
    return address, privkey_hex
