#!/opt/bin/python3
"""
Запуск:
    python3 klipsch.py
    python3 klipsch.py --tv 192.168.31.38 --soundbar-ip 192.168.31.64 --interval 1
"""

import argparse
import json
import socket
import ssl
import sys
import time

CAST_PORT = 8009
SOURCE_ID = "sender-0"
RECEIVER_ID = "receiver-0"
NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"

SOUNDBAR_WAKE_PATH = (
    "/api/setData?path=powermanager%3AtargetRequest&roles=activate"
    "&value=%7B%22target%22%3A%22online%22%2C%22reason%22%3A%22userActivity%22%7D"
)

CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ---------------------- минимальный protobuf (CastMessage) ----------------------

def _varint_encode(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _varint_decode(data: bytes, i: int):
    result = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _tag(field_num: int, wire_type: int) -> bytes:
    return _varint_encode((field_num << 3) | wire_type)


def _field_str(field_num: int, s: str) -> bytes:
    data = s.encode("utf-8")
    return _tag(field_num, 2) + _varint_encode(len(data)) + data


def _field_varint(field_num: int, v: int) -> bytes:
    return _tag(field_num, 0) + _varint_encode(v)


def encode_cast_message(source_id: str, destination_id: str, namespace: str, payload: dict) -> bytes:
    body = b""
    body += _field_varint(1, 0)
    body += _field_str(2, source_id)
    body += _field_str(3, destination_id)
    body += _field_str(4, namespace)
    body += _field_varint(5, 0)
    body += _field_str(6, json.dumps(payload))
    return body


def decode_cast_message(data: bytes) -> dict:
    fields = {}
    i = 0
    while i < len(data):
        tag_val, i = _varint_decode(data, i)
        field_num = tag_val >> 3
        wire_type = tag_val & 0x7
        if wire_type == 0:
            val, i = _varint_decode(data, i)
        elif wire_type == 2:
            length, i = _varint_decode(data, i)
            val = data[i:i + length]
            i += length
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields[field_num] = val
    return fields


# ------------------------------ сетевой уровень ------------------------------

def _send_message(sock, namespace, payload, destination_id=RECEIVER_ID):
    msg = encode_cast_message(SOURCE_ID, destination_id, namespace, payload)
    sock.sendall(len(msg).to_bytes(4, "big") + msg)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed while reading")
        buf += chunk
    return buf


def _recv_message(sock, timeout):
    sock.settimeout(timeout)
    length_bytes = _recv_exact(sock, 4)
    length = int.from_bytes(length_bytes, "big")
    body = _recv_exact(sock, length)
    fields = decode_cast_message(body)
    raw_payload = fields.get(6, b"")
    if raw_payload:
        try:
            return json.loads(raw_payload.decode("utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def get_tv_standby(host: str, timeout: float = 1.5):
    """
    Возвращает:
      True  - TV в standby (или явно неактивен)
      False - TV включен (isActiveInput/onscreen активен)
      None  - порт недоступен / нет ответа / любая сетевая ошибка
              (считаем как "выключен" для целей будильника)

    ВАЖНО: ловим здесь буквально любое исключение (Exception), а не только
    socket.timeout/ConnectionError. За долгую работу рано или поздно
    вылезет ssl.SSLError / ssl.SSLEOFError (TV оборвал TLS не по протоколу)
    или OSError на sendall - если их не поймать, необработанное исключение
    убьёт весь процесс. Это и было причиной "умирает спустя время".
    """
    sock = None
    try:
        raw = socket.create_connection((host, CAST_PORT), timeout=timeout)
        sock = CTX.wrap_socket(raw, server_hostname=host)

        _send_message(sock, NS_CONNECTION, {"type": "CONNECT"})
        _send_message(sock, NS_HEARTBEAT, {"type": "PING"})
        _send_message(sock, NS_RECEIVER, {"requestId": 1, "type": "GET_STATUS"})

        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            payload = _recv_message(sock, remaining)
            if isinstance(payload, dict) and payload.get("type") == "RECEIVER_STATUS":
                status = payload.get("status", {})
                return bool(status.get("isStandBy", True))
        return None
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def wake_soundbar(soundbar_ip: str, connect_timeout: float = 2.0) -> bool:
    """
    Fire-and-forget: устанавливаем TCP-соединение, отправляем HTTP-запрос
    и сразу закрываем сокет, НЕ дожидаясь ответа soundbar. Это нужно, чтобы
    сам soundbar (который в этот момент ещё просыпается) не тормозил наш
    секундный цикл опроса TV своим долгим ответом/таймаутом.
    ОС гарантированно доставит уже отправленные байты по TCP даже после
    close() с нашей стороны, так что сам факт отправки не теряется.
    """
    try:
        sock = socket.create_connection((soundbar_ip, 80), timeout=connect_timeout)
        request = (
            f"GET {SOUNDBAR_WAKE_PATH} HTTP/1.1\r\n"
            f"Host: {soundbar_ip}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode("utf-8"))
        sock.close()
        return True
    except Exception as e:
        print(f"    [!] не удалось отправить wake на soundbar: {e}", file=sys.stderr)
        return False


# ---------------------------------- main loop ----------------------------------


def main():
    parser = argparse.ArgumentParser(description="TV->Soundbar wake watcher")
    parser.add_argument("--tv", default="192.168.31.38", help="IP телевизора")
    parser.add_argument("--soundbar-ip", default="192.168.31.64", help="IP soundbar")
    parser.add_argument("--debug", action="store_true", help="Подробный вывод")
    args = parser.parse_args()

    tv_was_on = False

    print(f"Watching {args.tv}:8009 -> {args.soundbar_ip}")

    while True:
        try:
            interval = 5.0 if tv_was_on else 1.0
            loop_start = time.time()

            is_standby = get_tv_standby(
                args.tv,
                timeout=min(2.0, interval * 1.5)
            )

            ts = time.strftime("%H:%M:%S")

            if is_standby is None:
                if args.debug:
                    print(f"[{ts}] TV unreachable")
                if tv_was_on:
                    print(f"[{ts}] TV OFF")
                tv_was_on = False

            elif is_standby:
                if args.debug:
                    print(f"[{ts}] TV isStandBy=true")
                if tv_was_on:
                    print(f"[{ts}] TV OFF")
                tv_was_on = False

            else:
                if not tv_was_on:
                    print(f"[{ts}] TV ON -> waking soundbar")
                    ok = wake_soundbar(args.soundbar_ip)
                    if args.debug:
                        print(f"Wake request: {'OK' if ok else 'FAILED'}")
                elif args.debug:
                    print(f"[{ts}] TV ON")
                tv_was_on = True

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, interval - elapsed))

        except Exception as e:
            # последний рубеж: что бы ни случилось в итерации, watcher не
            # должен падать - логируем и живём дальше следующей итерацией
            print(f"[!] неожиданная ошибка в цикле: {e}", file=sys.stderr)
            time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено")
