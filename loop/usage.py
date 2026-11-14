"""Provider-reported token accounting, independent of resumable conversation context."""
import time

FIELDS = ('total_tokens', 'input_tokens', 'output_tokens', 'cached_tokens', 'cache_creation_tokens')


def number(value):
    return value if type(value) is int and value >= 0 else None


def normalize(provider, usage):
    usage = usage if isinstance(usage, dict) else {}
    incoming = number(usage.get('input_tokens'))
    outgoing = number(usage.get('output_tokens'))
    cached = number(usage.get('cached_input_tokens'))
    if cached is None:
        cached = number(usage.get('cache_read_input_tokens'))
    created = number(usage.get('cache_creation_input_tokens'))
    if provider == 'claude' and any(v is not None for v in (incoming, cached, created)):
        incoming = (incoming or 0) + (cached or 0) + (created or 0)
    total = number(usage.get('total_tokens'))
    if total is None and (incoming is not None or outgoing is not None):
        total = (incoming or 0) + (outgoing or 0)
    return dict(zip(FIELDS, (total, incoming, outgoing, cached, created)))


def empty():
    return {'values': {key: 0 for key in FIELDS}, 'reports': {key: 0 for key in FIELDS},
            'reported_calls': 0, 'unreported_calls': 0, 'historical_tokens': 0,
            'last_reported_at': None, 'latest': None}


def baseline(state, role):
    result = empty()
    result['unreported_calls'] = sum(value.get('calls', 0) for key, value in state.get('cost', {}).items()
                                     if key.startswith(role + ':'))
    if role == 'worker':
        previous = sum(value for value in state.get('tokens', {}).values() if number(value) is not None)
        result['values']['total_tokens'] = previous
        result['historical_tokens'] = previous
        if previous:
            result['reports']['total_tokens'] = 1
    return result


def add(metrics, values, now, provider, model):
    reported = any(value is not None for value in values.values())
    metrics['reported_calls' if reported else 'unreported_calls'] += 1
    for key, value in values.items():
        if value is not None:
            metrics['values'][key] += value
            metrics['reports'][key] += 1
    if reported:
        metrics['last_reported_at'] = now
        metrics['latest'] = {**values, 'provider': provider, 'model': model, 'at': now}


def record(state, role, provider, usage, event_id, model=None, now=None):
    """Record once per durable attempt/call; preserve counters across model/session changes."""
    now = time.time() if now is None else now
    ledger = state.setdefault('token_usage', {'version': 1, 'roles': {}})
    roles = ledger['roles']
    if role not in roles:
        roles[role] = {**baseline(state, role), 'events': [], 'by_provider': {}, 'by_model': {}}
        providers = {key.split(':', 1)[1] for key in state.get('cost', {}) if key.startswith(role + ':')}
        if role == 'worker':
            providers.update(state.get('tokens', {}))
        for previous_provider in providers:
            previous_state = {'cost': {key: value for key, value in state.get('cost', {}).items()
                                      if key == role + ':' + previous_provider},
                              'tokens': {previous_provider: state.get('tokens', {}).get(previous_provider, 0)}}
            row = baseline(previous_state, role)
            if row['unreported_calls'] or row['historical_tokens']:
                roles[role]['by_provider'][previous_provider] = {**row, 'provider': previous_provider}
                roles[role]['by_model'][previous_provider + ':(earlier usage)'] = {
                    **row, 'values': dict(row['values']), 'reports': dict(row['reports']),
                    'provider': previous_provider, 'model': None}

    metrics = roles[role]
    if event_id in metrics['events']:
        return
    metrics['events'] = (metrics['events'] + [event_id])[-4096:]
    values = normalize(provider, usage)
    add(metrics, values, now, provider, model)
    provider_row = metrics['by_provider'].setdefault(provider, {**empty(), 'provider': provider})
    model_key = provider + ':' + (model or '(default)')
    model_row = metrics['by_model'].setdefault(model_key, {**empty(), 'provider': provider, 'model': model})
    add(provider_row, values, now, provider, model)
    add(model_row, values, now, provider, model)


def public(metrics):
    calls = metrics['reported_calls'] + metrics['unreported_calls']
    result = {key: metrics['values'][key] if metrics['reports'][key] or (calls == 0 and not metrics['historical_tokens']) else None for key in FIELDS}
    result.update(reported_calls=metrics['reported_calls'], unreported_calls=metrics['unreported_calls'],
                  historical_tokens=metrics['historical_tokens'], calls=calls,
                  partial=bool(metrics['unreported_calls'] or metrics['historical_tokens']),
                  last_reported_at=metrics['last_reported_at'], latest=metrics['latest'])
    return result


def role_usage(state, role):
    stored = state.get('token_usage', {}).get('roles', {}).get(role)
    metrics = stored if stored is not None else baseline(state, role)
    result = public(metrics)
    result['by_provider'] = [{**public(row), 'provider': key}
                             for key, row in metrics.get('by_provider', {}).items()]
    result['by_model'] = [{**public(row), 'provider': row['provider'], 'model': row['model']}
                          for row in metrics.get('by_model', {}).values()]
    return result


def combine(parts):
    parts = list(parts)
    active = [part for part in parts if part['calls'] or part.get('historical_tokens')]
    result = {}
    for key in FIELDS:
        values = [part[key] for part in active if number(part.get(key)) is not None]
        result[key] = sum(values) if values else (None if active else 0)
    for key in ('reported_calls', 'unreported_calls', 'historical_tokens', 'calls'):
        result[key] = sum(part.get(key, 0) for part in parts)
    result['partial'] = any(part.get('partial', False) for part in parts)
    dated = [part for part in parts if part.get('last_reported_at') is not None]
    latest = max(dated, key=lambda part: part['last_reported_at']) if dated else None
    result['last_reported_at'] = latest['last_reported_at'] if latest else None
    result['latest'] = latest['latest'] if latest else None
    for group in ('by_provider', 'by_model'):
        rows = {}
        for part in parts:
            for row in part.get(group, []):
                identity = (row['provider'], row.get('model')) if group == 'by_model' else row['provider']
                rows.setdefault(identity, []).append(row)
        result[group] = []
        for identity, members in sorted(rows.items(), key=lambda item: str(item[0])):
            aggregate = combine(members)
            aggregate.pop('by_provider'); aggregate.pop('by_model')
            aggregate['provider'] = members[0]['provider']
            if group == 'by_model':
                aggregate['model'] = members[0].get('model')
            result[group].append(aggregate)
    return result


def agent_usage(state, config):
    worker, reviewer, supervisor = (role_usage(state, role) for role in ('worker', 'reviewer', 'supervisor'))
    return {'worker': worker, 'reviewer': reviewer, 'supervisor': supervisor,
            'agent': combine([worker, reviewer]), 'context_tokens': state.get('context_tokens', 0),
            'context_reset_at': config.get('reset_at_tokens'), 'worker_budget': config.get('max_worker_tokens'),
            'active_turn': bool(state.get('attempt'))}
