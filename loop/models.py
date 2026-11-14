"""Local model catalogue; discovery never starts a paid model turn."""
import json
import os
from pathlib import Path

from .storage import atomic_json

PROVIDERS = ('codex', 'claude', 'antigravity', 'muse')


def catalog(home):
    path = Path(home) / 'models.json'
    try:
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    result = {p: {'models': [], 'source': 'Saved custom models', 'updated_at': None} for p in PROVIDERS}
    for provider in PROVIDERS:
        entry = saved.get(provider, {})
        entries = entry.get('models', []) if isinstance(entry, dict) else []
        result[provider]['models'] = [m for m in entries if isinstance(m, dict)
                                      and isinstance(m.get('id'), str) and (provider == 'codex' or m.get('custom'))]
    previous = result['codex']['models']
    cache = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
    try:
        data = json.loads(cache.read_text())
        if not isinstance(data.get('models'), list):
            raise ValueError('Invalid model cache')
        discovered = []
        hidden = set()
        for model in data['models']:
            if not isinstance(model, dict) or not isinstance(model.get('slug'), str):
                continue
            if model.get('visibility') != 'list':
                hidden.add(model['slug'])
                continue
            discovered.append({'id': model['slug'], 'label': model.get('display_name') or model['slug'],
                'custom': False, 'previously_seen': False,
                'efforts': [e['effort'] for e in model.get('supported_reasoning_levels', [])
                            if isinstance(e, dict) and isinstance(e.get('effort'), str)]})
        ids = {m['id'] for m in discovered} | hidden
        # Multiple CLI clients can overwrite the shared cache with smaller lists.
        # Retain older choices without claiming they are currently advertised.
        retained = [{**m, 'previously_seen': not m.get('custom', False)}
                    for m in previous if m['id'] not in ids]
        result['codex']['models'] = discovered + retained
        result['codex'].update(source='Codex local catalogue', updated_at=data.get('fetched_at'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        if isinstance(saved.get('codex'), dict):
            result['codex'] = {**saved['codex'], 'models': previous}
    atomic_json(path, result)
    return result


def remember(home, provider, model):
    if provider not in PROVIDERS or not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError('Choose a provider and a model ID of 1–200 characters')
    model = model.strip()
    result = catalog(home)
    if not any(m['id'] == model for m in result[provider]['models']):
        result[provider]['models'].append({'id': model, 'label': model, 'custom': True})
        atomic_json(Path(home) / 'models.json', result)
    return result
