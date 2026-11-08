"""Pure local views over immutable raw snapshots. No HTTP calls."""
from copy import deepcopy
from decimal import Decimal, localcontext
from collections import Counter, defaultdict
import re
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import uuid

if __package__ and '.' in __package__:
    from ..messages import ATTRIBUTION, CREDIT
else:
    from messages import ATTRIBUTION, CREDIT

PROCESSING_V2 = 'XIT001 raw records preserved; local filtering, paging and grouped statistics by this plugin. CSV formula cells are escaped.'


class ViewError(ValueError):
    """A fixed local-view diagnostic; remains compatible with ValueError callers."""
    def __init__(self, error_type, detail):
        super().__init__(detail)
        self.error_type, self.detail = error_type, detail


def provenance():
    return {'attribution': ATTRIBUTION, 'attribution_url': 'https://www.reinfolib.mlit.go.jp/',
            'processing': PROCESSING_V2, 'credit': CREDIT}

TEXT_FILTERS = {'district_name': 'DistrictName', 'type': 'Type', 'floor_plan': 'FloorPlan',
                'price_category': 'PriceCategory', 'period': 'Period'}
RANGES = {'area': 'Area', 'price': 'TradePrice', 'building_year': 'BuildingYear'}


def number(raw):
    """Parse only exact nonnegative ASCII decimals; never discard the raw value."""
    if not isinstance(raw, str) or len(raw) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', raw):
        return None
    return Decimal(raw)


def building_year(raw):
    if not isinstance(raw, str):
        return None
    match = re.fullmatch(r'([0-9]{4})(?:年)?', raw)
    if match:
        return int(match[1])
    match = re.fullmatch(r'(明治|大正|昭和|平成|令和)(元|[0-9]{1,2})年', raw)
    if match:
        era, year = match.groups()
        n = 1 if year == '元' else int(year)
        base, max_year = {'明治': (1867, 45), '大正': (1911, 15), '昭和': (1925, 64),
                          '平成': (1988, 31), '令和': (2018, 99)}[era]
        return base + n if 1 <= n <= max_year else None
    return None


def validate_filters(filters):
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ViewError('invalid_argument', 'invalid_filters')
    allowed = set(TEXT_FILTERS) | {f'{key}_{bound}' for key in RANGES for bound in ('min', 'max')}
    if set(filters) - allowed:
        raise ViewError('invalid_argument', 'unknown_filter')
    for key, value in filters.items():
        if key in TEXT_FILTERS:
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ViewError('invalid_argument', 'invalid_filter_text')
        elif key.startswith('building_year'):
            if type(value) is not int or not 1800 <= value <= 9999:
                raise ViewError('invalid_argument', 'invalid_year_filter')
        elif number(value) is None:
            raise ViewError('invalid_argument', 'invalid_numeric_filter')
    for key in RANGES:
        low, high = filters.get(key + '_min'), filters.get(key + '_max')
        if low is not None and high is not None:
            left = low if key == 'building_year' else number(low)
            right = high if key == 'building_year' else number(high)
            if left is None or right is None:
                raise ViewError('invalid_argument', 'invalid_numeric_filter')
            if left > right:
                raise ViewError('invalid_argument', 'inverted_range')
    return deepcopy(filters)


def select(snapshot, filters=None):
    return select_with_diagnostics(snapshot, filters)[0]


def select_with_diagnostics(snapshot, filters=None):
    """Return matches plus per-range reasons; one row can fail several fields."""
    filters = validate_filters(filters)
    active = [key for key in RANGES if key + '_min' in filters or key + '_max' in filters]
    diagnostics = {key: {'did_not_match_n': 0, 'unparseable_n': 0, 'missing_n': 0} for key in active}
    matched = []
    for row in snapshot['records']:
        keep = all(row.get(column) == filters[key]
                   for key, column in TEXT_FILTERS.items() if key in filters)
        for key in active:
            raw = row.get(RANGES[key])
            value = building_year(raw) if key == 'building_year' else number(raw)
            if value is None:
                keep = False
                reason = 'missing_n' if raw in (None, '') else 'unparseable_n'
                diagnostics[key][reason] += 1
                continue
            field_matches = True
            for bound in ('min', 'max'):
                limit = filters.get(key + '_' + bound)
                if limit is not None:
                    limit = limit if key == 'building_year' else number(limit)
                    if limit is None:
                        raise ViewError('invalid_argument', 'invalid_numeric_filter')
                    if (bound == 'min' and value < limit) or (bound == 'max' and value > limit):
                        field_matches = False
            if not field_matches:
                keep = False
                diagnostics[key]['did_not_match_n'] += 1
        if keep:
            matched.append(row)
    return matched, diagnostics


def _filter_warnings(diagnostics):
    unknown = any(item['unparseable_n'] or item['missing_n'] for item in diagnostics.values())
    return ['unknown_values_excluded_from_range_filters'] if unknown else []


def _label(value):
    return value if isinstance(value, str) else '<unknown>'


def statistics(rows):
    groups = defaultdict(list)
    for row in rows:
        key = tuple(_label(row.get(k)) for k in ('Type', 'PriceCategory', 'Period'))
        groups[key].append(row)
    result = []
    for (kind, source, period), group in sorted(groups.items()):
        prices = sorted(value for row in group if (value := number(row.get('TradePrice'))) is not None)
        missing = sum(row.get('TradePrice') in (None, '') for row in group)
        median = None
        if prices:
            i = len(prices) // 2
            with localcontext() as ctx:
                ctx.prec = 150
                value = prices[i] if len(prices) % 2 else (prices[i - 1] + prices[i]) / 2
                median = format(value, 'f')
        result.append({'type': kind, 'price_category': source, 'period': period,
                       'count': len(group), 'valid_n': len(prices), 'missing_n': missing,
                       'parse_failed_n': len(group) - len(prices) - missing,
                       'median_trade_price': median, 'currency': 'JPY'})
    return result


def page(snapshot, *, offset=0, limit=20, filters=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ViewError('invalid_argument', 'invalid_paging')
    rows, diagnostics = select_with_diagnostics(snapshot, filters)
    selected = rows[offset:offset + limit]
    end = offset + len(selected)
    more = end < len(rows)
    return {'ok': True, 'schema_version': 1,
            'snapshot_id': snapshot['snapshot_id'], 'query': deepcopy(snapshot['query']),
            'effective_scope': deepcopy(snapshot['query']), 'fetched_at': snapshot['fetched_at'],
            'upstream_count': len(snapshot['records']), 'matched_count': len(rows),
            'returned_count': len(selected), 'offset': offset,
            'next_offset': end if more else None, 'has_more': more,
            'filters': validate_filters(filters),
            'filtered_out_n': len(snapshot['records']) - len(rows),
            'filter_diagnostics': diagnostics,
            'statistics_scope': 'all_matched_records',
            'source_counts': dict(Counter(_label(row.get('PriceCategory')) for row in rows)),
            'statistics': statistics(rows),
            'warnings': list(snapshot.get('warnings', [])) + _filter_warnings(diagnostics) + ['Counts are API-covered records; sources can contain duplicate transactions.'],
            'records': deepcopy(selected), **provenance()}


def _csv_cell(value):
    text = value if isinstance(value, str) else ('' if value is None else json.dumps(value, ensure_ascii=False))
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def _publish(root, name, content):
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        target = root / name
        os.link(temporary, target)  # Exclusive publish: never overwrite an existing export.
        return target
    finally:
        os.unlink(temporary)


def export(snapshot, root, *, format='json', filters=None):
    if format not in ('json', 'csv'):
        raise ViewError('invalid_argument', 'invalid_export_format')
    filters = validate_filters(filters)
    root = Path(root)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ViewError('storage', 'symlink_export_root')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    rows, diagnostics = select_with_diagnostics(snapshot, filters)
    meta = {k: deepcopy(v) for k, v in snapshot.items() if k != 'records'}
    meta.update(provenance())
    meta.update({'matched_count': len(rows), 'filters': filters,
                 'filtered_out_n': len(snapshot['records']) - len(rows),
                 'filter_diagnostics': diagnostics,
                 'warnings': list(snapshot.get('warnings', [])) + _filter_warnings(diagnostics) + ['API-covered records; sources can contain duplicate transactions.']})
    if format == 'json':
        content = json.dumps({**meta, 'records': rows}, ensure_ascii=False, allow_nan=False).encode('utf-8')
    else:
        stream = io.StringIO(newline='')
        fields = sorted({field for row in rows for field in row})
        writer = csv.writer(stream)
        writer.writerow([_csv_cell(field) for field in fields])
        writer.writerows([_csv_cell(row.get(field)) for field in fields] for row in rows)
        content = stream.getvalue().encode('utf-8-sig')
    if len(content) > 32_000_000:
        raise ViewError('response_too_large', 'export_too_large')
    name = uuid.uuid4().hex
    path = _publish(root, name + '.' + format, content)
    try:
        manifest = _publish(root, name + '.metadata.json', json.dumps(meta, ensure_ascii=False).encode('utf-8'))
    except Exception:
        path.unlink()
        raise
    return {'ok': True, 'snapshot_id': snapshot['snapshot_id'], 'format': format,
            'count': len(rows), 'path': str(path.resolve()), 'manifest_path': str(manifest.resolve()),
            'sha256': hashlib.sha256(content).hexdigest(), **provenance()}
