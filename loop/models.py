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
    except (OSError, ValueError):
        saved = {}
    result = {p: {'models': [], 'source': 'Saved custom models', 'updated_at': None} for p in PROVIDERS}
    for provider in PROVIDERS:
        result[provider]['models'] = [m for m in saved.get(provider, {}).get('models', [])
                                      if isinstance(m, dict) and m.get('id') and m.get('custom')]
    cache = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
    try:
        data = json.loads(cache.read_text())
        discovered = [{'id': m['slug'], 'label': m.get('display_name') or m['slug'], 'custom': False, 'efforts': [e['effort'] for e in m.get('supported_reasoning_levels', []) if isinstance(e, dict) and isinstance(e.get('effort'), str)]}
                      for m in data['models'] if isinstance(m, dict) and isinstance(m.get('slug'), str)
                      and m.get('visibility') == 'list']
        ids = {m['id'] for m in discovered}
        result['codex']['models'] = discovered + [m for m in result['codex']['models'] if m['id'] not in ids]
        result['codex'].update(source='Codex local catalogue', updated_at=data.get('fetched_at'))
    except (OSError, ValueError, KeyError, TypeError):
        # Preserve the last discovered catalogue if the CLI cache is unavailable.
        if saved.get('codex'):
            result['codex'] = saved['codex']
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
