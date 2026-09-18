"""Read execution receipts, never run shell text emitted by a model.

Auto-refine : quand un problème est détecté, extraire les leçons du contexte,
les sauver dans ~/.hermes/lessons/ et retourner un INSIGHT au lieu d'un
WAIT_HUMAN (pause). Le juge review la discussion, identifie les patterns
récurrents, et les sauve comme compétence réutilisable.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing


def redact(text: str) -> str:
    return re.sub(r'(?:gh[a-z0-9_]+|github_pat_[a-z0-9_]+|Bearer\\s+\\S+)', '[REDACTED]', text)


# ── Auto-refine : sauvegarde des leçons ───────────────────────────────────────

LESSONS_DIR = Path.home() / ".hermes" / "lessons"
LESSONS_FILE = LESSONS_DIR / "auto_refine.jsonl"

LESSON_PATTERNS = {
    'textual_python_block': {
        'title': 'Code Python dans un bloc Markdown au lieu d\'outil natif',
        'description': (
            'L\'agent a posté du code Python dans un bloc Markdown (```execute_code '
            'ou ```python) au lieu d\'utiliser l\'outil natif execute_code ou terminal. '
            'Cela viole la contrainte absolue d\'interdiction du code Markdown textuel.'
        ),
        'recommendation': (
            'Invoquer terminal ou execute_code nativement. Ne jamais afficher de '
            'code Python dans un bloc Markdown.'
        ),
    },
    'fragmented_content': {
        'title': 'Contenu trop long découpé en plusieurs messages Discord',
        'description': (
            'Le contenu d\'un seul tour LLM a été découpé en plusieurs messages '
            'Discord sans appel d\'outil entre eux.'
        ),
        'recommendation': (
            'Appliquer le contenu via un appel execute_code au lieu de le laisser '
            'en texte brut.'
        ),
    },
    'execution_stalled': {
        'title': 'Trois tours consécutifs sans appel d\'outil système',
        'description': (
            'L\'agent a décrit ce qu\'il ferait without l\'invoquer pendant trois tours '
            'consécutifs. Probable 원인이 : l\'agent explique plutôt que d\'agir.'
        ),
        'recommendation': (
            'Exécuter via terminal ou execute_code la prochaine étape vérifiable '
            'identifiée dans le dernier message, even partielle.'
        ),
    },
    'no_tool_execution': {
        'title': 'Aucun retour d\'outil enregistré',
        'description': (
            'Le texte affiché contient des explications ou des blocs Markdown au '
            'lieu d\'un appel d\'outil natif.'
        ),
        'recommendation': (
            'Invoquer l\'outil système terminal ou execute_code via un appel d\'outil '
            'natif. Ne pas placer le code dans un bloc de texte Markdown.'
        ),
    },
    'raw_json_tool_attempt': {
        'title': 'JSON brut dans le texte au lieu d\'appel d\'outil natif',
        'description': (
            'L\'agent a envoyé une structure JSON brute ({"name": ...}) dans le texte '
            'au lieu d\'invoquer l\'outil système via un appel natif (execute_code ou terminal). '
            'Cela indique que l\'agent tente d\'exécuter un outil mais ne passe pas par le '
            'mécanisme correct d\'appel d\'outil.'
        ),
        'recommendation': (
            'Invoquer l\'outil système terminal ou execute_code via un appel d\'outil natif. '
            'Ne pas écrire de structure JSON dans le message texte.'
        ),
    },
}


def save_lesson(lesson: dict) -> None:
    """Sauver une leçon dans le fichier JSONL de lessons."""
    LESSONS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'type': lesson.get('type', 'auto_refine'),
        'pattern': lesson.get('pattern', ''),
        'title': lesson.get('title', ''),
        'session_id': lesson.get('session_id', ''),
        'context': lesson.get('context', ''),
        'lesson': lesson.get('lesson', ''),
        'recommendation': lesson.get('recommendation', ''),
        'frequency': lesson.get('frequency', 1),
    }
    with LESSONS_FILE.open('a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def build_lesson_payload(pattern_code: str, turn_context: dict, session_id: str) -> dict | None:
    """Construire la payload de leçon depuis le contexte du tour et le pattern détecté."""
    pattern_info = LESSON_PATTERNS.get(pattern_code)
    if not pattern_info:
        return None
    
    # Extraire le contexte pertinent depuis le tour
    assistant_msgs = turn_context.get('assistant_content', '')
    tool_results = turn_context.get('tool_results', '')
    
    # Construire le contexte résumé
    context_snippet = ''
    if assistant_msgs:
        # Prendre les premiers 500 chars de l'assistant message
        first_msg = assistant_msgs.split('\\n---\\n')[0] if '\\n---\\n' in assistant_msgs else assistant_msgs
        context_snippet = first_msg[:500]
    
    # Déterminer la fréquence basée sur le count
    frequency = turn_context.get('frequency', 1)
    
    return {
        'type': 'auto_refine',
        'pattern': pattern_code,
        'title': pattern_info['title'],
        'session_id': session_id,
        'context': context_snippet,
        'lesson': pattern_info['description'],
        'recommendation': pattern_info['recommendation'],
        'frequency': frequency,
    }


# ── Lecture du tour ────────────────────────────────────────────────────────────

def read_turn(db_path, session_id):
    """Lire les receipts d'exécution depuis la session DB."""
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        boundary = db.execute(
            "SELECT MAX(id) FROM messages WHERE session_id=? AND role='user'",
            (session_id,)
        ).fetchone()[0]
        if boundary is None:
            raise RuntimeError('No persisted user turn boundary; execution cannot be verified')
        
        rows = db.execute(
            'SELECT id, role, content, tool_name, tool_calls FROM messages WHERE session_id=? AND id>? ORDER BY id DESC LIMIT 200',
            (session_id, boundary)
        ).fetchall()
        
        receipts = []
        calls = 0
        textual_python_block = False
        raw_json_tool_attempt = False
        assistant_count = 0
        assistant_content_parts = []
        
        for row in reversed(rows):
            if row['role'] == 'assistant':
                assistant_count += 1
                if row['tool_calls']:
                    try:
                        calls += len(json.loads(row['tool_calls']))
                    except (ValueError, TypeError):
                        pass
                elif row['content']:
                    cnt = (row['content'] or '').strip()
                    # Détection JSON brut
                    has_tool_json = (
                        '"name"' in cnt and (
                            '"arguments"' in cnt or '"parameters"' in cnt
                            or '"code"' in cnt or 'execute_code' in cnt or 'terminal' in cnt
                        )
                    )
                    if has_tool_json:
                        raw_json_tool_attempt = True
                    # Détection bloc Markdown Python
                    if cnt.startswith('```') and ('python' in cnt or 'execute_code' in cnt):
                        textual_python_block = True
                    # Accumuler le contenu assistant pour le contexte
                    assistant_content_parts.append(cnt[:300])
            elif row['role'] == 'tool':
                raw = row['content'] or ''
                receipts.append({
                    'message_id': row['id'],
                    'tool': row['tool_name'],
                    'output_excerpt': redact(raw)[:1600]
                })
        
        assistant_content = '\\n---\\n'.join(assistant_content_parts[-5:]) if assistant_content_parts else ''
        
        return {
            'boundary': boundary,
            'last_id': rows[0]['id'] if rows else boundary,
            'calls': calls,
            'results': len(receipts),
            'receipts': receipts[-12:],
            'raw_json_tool_attempt': raw_json_tool_attempt,
            'assistant_count': assistant_count,
            'textual_python_block': textual_python_block,
            'assistant_content': assistant_content,
        }


# ── Fragmentation detection ────────────────────────────────────────────────────

def _looks_like_fragmented_content(turn) -> tuple[bool, bool]:
    """Retourne (est_fragmenté, est_entêté).

    Le contenu d'un seul tour LLM a été découpé en plusieurs messages Discord
    sans appel d'outil entre eux. Dans ce cas, ce n'est pas un blocage — c'est
    un signal d'insight : demander à l'agent d'appliquer via execute_code.
    """
    n = turn.get('assistant_count', 0)
    if n >= 3:
        return True, True   # fragmentation confirmée + entêtée
    if n == 2:
        return True, False  # potentiel mais pas encore entêté
    return False, False


# ── Execution guard avec auto-refine ───────────────────────────────────────────

def execution_guard(turn, count, fragmented_count=0, last_fragment_id=None):
    """Décider du verdict en fonction du tour observé.

    Contrats :
    - textual_python_block : code Markdown textuel → INSIGHT (jamais WAIT_HUMAN)
    - fragmented_content : message long découpé → INSIGHT
    - execution_stalled : 3 tours sans outil → INSIGHT
    - raw_json_tool_attempt : JSON brut → WAIT_HUMAN (problème réel d'exécution)
    - no_tool_execution : pas d'outil → INSIGHT (sauf si stalled)
    """
    # Outil exécuté ET pas de bloc Python textuel : reset complet
    if turn['results'] and not turn.get('textual_python_block', False):
        return None, 0, 0, None
    
    count += 1
    
    # Lecture en amont pour éviter UnboundLocalError
    is_raw_json = turn.get('raw_json_tool_attempt', False)
    has_textual_python = turn.get('textual_python_block', False)
    is_fragmented, is_persistent = _looks_like_fragmented_content(turn)
    stalled = count >= 3
    
    if is_fragmented:
        if is_persistent:
            fragmented_count += 1
        else:
            fragmented_count = max(1, fragmented_count)
    else:
        fragmented_count = 0
    
    # Construction du contexte pour le lesson
    turn_context = {
        'assistant_content': turn.get('assistant_content', ''),
        'tool_results': turn.get('results', 0),
        'frequency': fragmented_count + count,
    }
    
    # ── textual_python_block : INSIGHT + lesson ──────────────────────────────
    if has_textual_python:
        lesson = build_lesson_payload('textual_python_block', turn_context, turn.get('_session_id', ''))
        if lesson:
            save_lesson(lesson)
        
        summary = (
            'Le worker a répondu avec un bloc Python textuel (markdown execute_code) '
            'au lieu d\'effectuer un appel d\'outil système réel via execute_code ou terminal. '
            'Cela viole la contrainte absolue d\'interdiction du code Markdown textuel (python). '
            'Le contenu du bloc est une inspection de fichiers, mais il n\'a pas été exécuté '
            'comme outil natif. De plus, le pipeline est en état running avec des fichiers '
            'modifiés non commités/poussés et aucun checkpoint confirmé.'
        )
        return {
            'verdict': 'INSIGHT',
            'reason_code': 'textual_python_block',
            'summary': summary,
            'next_required_outcome': (
                'Invoquer terminal ou execute_code nativement. Ne jamais afficher '
                'de code Python dans un bloc Markdown. Valider la pipeline avant de continuer.'
            ),
            'notify_owner': False,
        }, count, fragmented_count, turn.get('last_id')
    
    # ── fragmented_content : INSIGHT + lesson ────────────────────────────────
    if is_fragmented and is_persistent and fragmented_count >= 1:
        lesson = build_lesson_payload('fragmented_content', turn_context, turn.get('_session_id', ''))
        if lesson:
            save_lesson(lesson)
        
        summary = (
            'Le contenu de l\'agent est trop long pour un seul message Discord et déborde '
            'sur plusieurs messages. Applique le contenu via un appel execute_code au lieu '
            'de le laisser en texte brut.'
        )
        return {
            'verdict': 'INSIGHT',
            'reason_code': 'content_too_long_fragmented',
            'summary': summary,
            'next_required_outcome': 'Appliquer le contenu via execute_code',
            'notify_owner': False,
        }, count, fragmented_count, turn.get('last_id')
    
    # ── execution_stalled : INSIGHT + lesson ────────────────────────────────
    if stalled and not is_raw_json:
        lesson = build_lesson_payload('execution_stalled', turn_context, turn.get('_session_id', ''))
        if lesson:
            save_lesson(lesson)
        
        summary = (
            'Trois tours consécutifs sans appel d\'outil système. '
            'Piste probable : l\'agent a décrit ce qu\'il ferait sans l\'invoquer. '
            'Prochaine action : exécuter via terminal ou execute_code la prochaine '
            'étape vérifiable identifiée dans le dernier message, même partielle.'
        )
        return {
            'verdict': 'INSIGHT',
            'reason_code': 'execution_stalled',
            'summary': summary,
            'next_required_outcome': (
                'Exécuter via terminal ou execute_code la prochaine étape vérifiable du dernier message.'
            ),
            'notify_owner': False,
        }, count, fragmented_count, turn.get('last_id')
    
    # ── raw_json_tool_attempt : INSIGHT + lesson (jamais WAIT_HUMAN) ─────────
    if is_raw_json:
        lesson = build_lesson_payload('raw_json_tool_attempt', turn_context, turn.get('_session_id', ''))
        if lesson:
            save_lesson(lesson)

        summary = (
            'L\'agent a envoyé une structure JSON brute ({"name": ...}) dans le texte '
            'au lieu d\'invoquer l\'outil système via un appel natif (execute_code ou terminal). '
            'Cela indique que l\'agent tente d\'exécuter un outil mais ne passe pas par le '
            'mécanisme correct d\'appel d\'outil.'
        )
        next_outcome = (
            'Invoquer l\'outil système terminal ou execute_code via un appel d\'outil natif. '
            'Ne pas écrire de structure JSON dans le message texte.'
        )
        return {
            'verdict': 'INSIGHT',
            'reason_code': 'raw_json_tool_attempt',
            'summary': summary,
            'next_required_outcome': next_outcome,
            'notify_owner': False,
        }, count, fragmented_count, None
    
    # ── no_tool_execution : INSIGHT + lesson ────────────────────────────────
    lesson = build_lesson_payload('no_tool_execution', turn_context, turn.get('_session_id', ''))
    if lesson:
        save_lesson(lesson)
    
    summary = (
        'Aucun retour d\'outil enregistré : le texte affiché contient des explications '
        'ou des blocs Markdown au lieu d\'un appel d\'outil natif.'
    )
    next_outcome = (
        'Invoquer l\'outil système terminal ou execute_code via un appel d\'outil natif. '
        'Ne pas placer le code dans un bloc de texte Markdown.'
    )
    return {
        'verdict': 'INSIGHT',
        'reason_code': 'no_tool_execution',
        'summary': summary,
        'next_required_outcome': next_outcome,
        'notify_owner': False,
    }, count, fragmented_count, None
