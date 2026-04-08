"""
Betclic France odds scraper using gRPC-web (offering.begmedia.com).
Betclic France tennis odds scraper using gRPC-web.
Available categories (use category_id in get_match_markets):
  'ca_ten_top'  → Le Top           (featured, ~7 markets, default)
  'ca_ten_rslt' → Résultats        (~9 markets)
  'ca_ten_sts'  → Sets             (~12 markets)
  'ca_ten_gms'  → Jeux             (~12 markets, includes game handicap)
  'ca_ten_ptss' → Points & Service (aces totals + aces par joueur; breaks appear closer to match time)
"""

import struct
import threading
import time
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Category IDs par sport — pass as category_id to get_match_markets()
TENNIS_CATEGORIES: dict[str, str] = {
    'ca_ten_top':  'Le Top',
    'ca_ten_rslt': 'Résultats',
    'ca_ten_sts':  'Sets',
    'ca_ten_gms':  'Jeux',
    'ca_ten_ptss': 'Points & Service',
}

FOOTBALL_CATEGORIES: dict[str, str] = {
    'ca_foo_top':  'Le Top',
    'ca_foo_rslt': 'Résultats',
    'ca_foo_buts': 'Buts',
    'ca_foo_ah':   'Handicap asiatique',
    'ca_foo_mi':   'Mi-Temps',
}

BASKETBALL_CATEGORIES: dict[str, str] = {
    'ca_bsk_top':  'Le Top',
    'ca_bsk_rslt': 'Résultats',
    'ca_bsk_pts':  'Points',
    'ca_bsk_mi':   'Mi-Temps',
    'ca_bsk_qt':   'Quarts-Temps',
}

SPORT_CATEGORIES: dict[str, dict[str, str]] = {
    'tennis':     TENNIS_CATEGORIES,
    'football':   FOOTBALL_CATEGORIES,
    'basketball': BASKETBALL_CATEGORIES,
}

GRPC_BASE = 'https://offering.begmedia.com/web/offering.access.api'
MATCH_SERVICE = f'{GRPC_BASE}/offering.access.api.MatchService'


# ── Minimal protobuf encoder ──────────────────────────────────────────────────

def _encode_varint(value: int) -> bytes:
    result = b''
    while value > 0x7F:
        result += bytes([(value & 0x7F) | 0x80])
        value >>= 7
    return result + bytes([value])

def _encode_string(field_num: int, value: str) -> bytes:
    encoded = value.encode('utf-8')
    tag = (field_num << 3) | 2
    return bytes([tag]) + _encode_varint(len(encoded)) + encoded

def _encode_int64(field_num: int, value: int) -> bytes:
    tag = (field_num << 3) | 0
    return bytes([tag]) + _encode_varint(value)

def _grpc_web_frame(body: bytes) -> bytes:
    return bytes([0]) + struct.pack('>I', len(body)) + body


# ── Minimal protobuf decoder ──────────────────────────────────────────────────

def _decode_varint(data: bytes, pos: int):
    result = 0; shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos

def _decode_proto(data: bytes) -> dict:
    """Decode raw protobuf bytes into {field_num: value_or_list}."""
    result = {}; pos = 0
    while pos < len(data):
        try:
            tag_varint, pos = _decode_varint(data, pos)
        except Exception:
            break
        field_num = tag_varint >> 3
        wire_type = tag_varint & 7
        if wire_type == 0:
            v, pos = _decode_varint(data, pos)
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            if pos + length > len(data):
                break
            v = data[pos:pos+length]; pos += length
        elif wire_type == 5:
            if pos + 4 > len(data):
                break
            v = struct.unpack_from('<f', data, pos)[0]; pos += 4
        elif wire_type == 1:
            if pos + 8 > len(data):
                break
            v = struct.unpack_from('<d', data, pos)[0]; pos += 8
        else:
            break
        if field_num in result:
            if not isinstance(result[field_num], list):
                result[field_num] = [result[field_num]]
            result[field_num].append(v)
        else:
            result[field_num] = v
    return result

def _str(v) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except Exception:
            return ''
    return str(v) if v is not None else ''


def _extract_sels_f16(mkt: dict) -> list[dict]:
    """Extract selections from field[16] — used by simple 2/3-way markets."""
    sels_raw = mkt.get(16, [])
    if isinstance(sels_raw, bytes):
        sels_raw = [sels_raw]
    elif not isinstance(sels_raw, list):
        sels_raw = [sels_raw]
    sels = []
    for sb in sels_raw:
        if not isinstance(sb, bytes):
            continue
        sd = _decode_proto(sb)
        sname = _str(sd.get(10, b''))
        odds = sd.get(12, None)
        if sname and odds and isinstance(odds, float) and odds > 1.0:
            sels.append({'name': sname, 'odds': round(odds, 3)})
    return sels


def _extract_sels_f10(mkt: dict) -> list[dict]:
    """
    Extract selections from nested field[10] — used by totals/handicap markets.
    Structure: mkt[10] → groups; each group[1] → wrappers; each wrapper[1] → selection
    with field[10]=name, field[12]=odds (double).
    """
    f10 = mkt.get(10, [])
    if isinstance(f10, bytes):
        f10 = [f10]
    elif not isinstance(f10, list):
        f10 = [f10]
    sels = []
    for group_bytes in f10:
        if not isinstance(group_bytes, bytes):
            continue
        group = _decode_proto(group_bytes)
        wrappers = group.get(1, [])
        if isinstance(wrappers, bytes):
            wrappers = [wrappers]
        elif not isinstance(wrappers, list):
            wrappers = [wrappers]
        for wb in wrappers:
            if not isinstance(wb, bytes):
                continue
            wrapper = _decode_proto(wb)
            actual_bytes = wrapper.get(1)
            if not isinstance(actual_bytes, bytes):
                continue
            actual = _decode_proto(actual_bytes)
            sname = _str(actual.get(10, b''))
            odds = actual.get(12, None)
            if sname and odds and isinstance(odds, float) and odds > 1.0:
                sels.append({'name': sname, 'odds': round(odds, 3)})
    return sels


# ── gRPC-web HTTP transport ───────────────────────────────────────────────────

class BetclicClient:
    UA = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    GRPC_HEADERS = {
        'Content-Type': 'application/grpc-web+proto',
        'X-Grpc-Web': '1',
        'Accept': 'application/grpc-web+proto',
        'Origin': 'https://www.betclic.fr',
        'Referer': 'https://www.betclic.fr/',
        'X-BG-REGULATION': 'FR',
        'X-BG-Ref-Brand': 'BETCLIC',
        'X-BG-Ref-Regulator-Zone': 'FR',
        'X-BG-Ref-Platform': 'DESKTOP',
        'ngsw-bypass': '1',
    }

    def __init__(self):
        import urllib3
        urllib3.disable_warnings()
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.UA})
        self.session.headers.update(self.GRPC_HEADERS)

    def _stream(self, method: str, proto_body: bytes, max_seconds: float = 10.0, idle_secs: float = 0.5) -> bytes:
        """POST a gRPC-web request and return accumulated response bytes.

        Stops as soon as the connection closes naturally OR no new data
        has arrived for `idle_secs` (default 0.5s), whichever comes first.
        `max_seconds` is a hard safety cap.
        """
        frame = _grpc_web_frame(proto_body)
        content = bytearray()
        done = threading.Event()
        last_recv: list[float] = [time.monotonic()]

        def _read():
            try:
                resp = self.session.post(
                    f'{MATCH_SERVICE}/{method}',
                    data=frame, verify=False,
                    timeout=(5, max_seconds + 2), stream=True,
                )
                for chunk in resp.iter_content(chunk_size=None):
                    content.extend(chunk)
                    last_recv[0] = time.monotonic()
                    if len(content) > 2_000_000:
                        break
            except Exception:
                pass
            finally:
                done.set()

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        deadline = time.monotonic() + max_seconds
        while not done.is_set():
            if time.monotonic() > deadline:
                break
            if time.monotonic() - last_recv[0] > idle_secs:
                break
            time.sleep(0.05)
        t.join(timeout=2)
        return bytes(content)

    def _parse_frames(self, data: bytes) -> list[dict]:
        """Parse gRPC-web frames → list of decoded proto dicts (data frames only)."""
        frames = []
        i = 0
        while i < len(data):
            if i + 5 > len(data):
                break
            flags = data[i]
            length = struct.unpack('>I', data[i+1:i+5])[0]
            if i + 5 + length > len(data):
                break
            payload = data[i+5:i+5+length]
            i += 5 + length
            if flags == 0:  # data frame
                frames.append(_decode_proto(payload))
        return frames

    def get_matches(self, sport: str = 'tennis', language: str = 'fr', limit: int = 100) -> list[dict]:
        """
        Fetch all pre-match + live matches for a given sport.
        sport: 'tennis', 'football', 'basketball', ...
        Returns list of dicts: {match_id, title, start, is_live, competition_id,
                                 competition_name, open_market_count}.
        """
        proto_body = (
            _encode_string(1, sport) +
            _encode_string(3, language) +
            _encode_int64(4, 0) +
            _encode_int64(5, limit)
        )
        raw = self._stream('GetMatchesBySportWithNotifications', proto_body, max_seconds=10)
        frames = self._parse_frames(raw)
        matches = []
        for frame in frames:
            if 1 not in frame:
                continue
            inner = _decode_proto(frame[1])
            items = inner.get(3, [])
            if isinstance(items, bytes):
                items = [items]
            elif not isinstance(items, list):
                items = [items]
            for mb in items:
                if not isinstance(mb, bytes):
                    continue
                m = _decode_proto(mb)
                comp = {}
                if isinstance(m.get(8), bytes):
                    comp = _decode_proto(m[8])
                matches.append({
                    'match_id': m.get(1, 0),
                    'title': _str(m.get(2, b'')),
                    'start': _str(m.get(3, b'')),
                    'is_live': bool(m.get(4, 0)),
                    'competition_id': comp.get(1, 0),
                    'competition_name': _str(comp.get(2, b'')),
                    'open_market_count': m.get(7, 0),
                })
        return matches

    def get_tennis_matches(self, language: str = 'fr', limit: int = 100) -> list[dict]:
        """Alias for backward compatibility."""
        return self.get_matches('tennis', language, limit)

    def get_all_match_markets_for_categories(
        self,
        match_id: int,
        categories: dict[str, str],
        language: str = 'fr',
        max_seconds: float = 8.0,
    ) -> list[dict]:
        """
        Fetch markets for all categories in parallel (generic, any sport).
        categories: dict of {category_id: label}, e.g. FOOTBALL_CATEGORIES.
        Returns deduplicated list of {name, selections} dicts.
        """
        results: dict[str, dict] = {}
        threads = []
        lock = threading.Lock()

        def _fetch(cat_id: str):
            mkts = self.get_match_markets(match_id, language, max_seconds, category_id=cat_id)
            with lock:
                for m in mkts:
                    if m['name'] not in results:
                        results[m['name']] = m

        for cat in categories:
            t = threading.Thread(target=_fetch, args=(cat,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=max_seconds + 5)
        return list(results.values())

    def get_match_markets(
        self,
        match_id: int,
        language: str = 'fr',
        max_seconds: float = 8.0,
        category_id: str | None = None,
    ) -> list[dict]:
        """
        Fetch markets for one match via GetMatchWithNotification.
        Returns list of dicts: {name, selections: [{name, odds}]}.

        category_id: one of the TENNIS_CATEGORIES keys (e.g. 'ca_ten_gms').
          If None, returns the default "Le Top" (~7 featured markets).
        """
        proto_body = _encode_int64(1, match_id) + _encode_string(2, language)
        if category_id:
            proto_body += _encode_string(3, category_id)
        raw = self._stream('GetMatchWithNotification', proto_body, max_seconds=max_seconds)
        frames = self._parse_frames(raw)
        all_markets: dict[int, dict] = {}
        for frame in frames:
            if 1 not in frame:
                continue
            try:
                inner = _decode_proto(frame[1])
                match = _decode_proto(inner[1])
            except Exception:
                continue
            f11 = match.get(11, b'')
            subcats = f11 if isinstance(f11, list) else ([f11] if isinstance(f11, bytes) else [])
            for sc_bytes in subcats:
                if not isinstance(sc_bytes, bytes):
                    continue
                f11d = _decode_proto(sc_bytes)
                items = f11d.get(3, [])
                if isinstance(items, bytes):
                    items = [items]
                elif not isinstance(items, list):
                    items = [items]
                for item in items:
                    if not isinstance(item, bytes):
                        continue
                    mkt = _decode_proto(item)
                    mid = mkt.get(1, 0)
                    mname = _str(mkt.get(2, b''))

                    sels = _extract_sels_f16(mkt) or _extract_sels_f10(mkt)
                    if mid and mname and sels:
                        all_markets[mid] = {'name': mname, 'selections': sels}
        return list(all_markets.values())

    def get_all_match_markets(
        self,
        match_id: int,
        language: str = 'fr',
        max_seconds: float = 8.0,
        categories: list[str] | None = None,
    ) -> list[dict]:
        """
        Fetch markets for all (or specified) categories in parallel.
        categories: list of TENNIS_CATEGORIES keys; defaults to all 5.
        Returns deduplicated list of {name, selections} dicts.
        """
        if categories is None:
            categories = list(TENNIS_CATEGORIES.keys())
        results: dict[str, dict] = {}
        threads = []
        lock = threading.Lock()

        def _fetch(cat_id: str):
            mkts = self.get_match_markets(match_id, language, max_seconds, category_id=cat_id)
            with lock:
                for m in mkts:
                    key = m['name']
                    if key not in results:
                        results[key] = m

        for cat in categories:
            t = threading.Thread(target=_fetch, args=(cat,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=max_seconds + 5)
        return list(results.values())

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
