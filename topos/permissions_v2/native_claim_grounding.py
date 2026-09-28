"""Conservative entailment floor for machine-derived native recovery facts.

Literal object containment is not evidence for a relation. These complete
first-person statements are the currently supported grammar, not an NLP truth
oracle. Questions, plans, negation, quotations, extra clauses and uncertain
forms remain unsupported. Source authorship and privacy are separate checks.
"""
import re


_FORMS = {
    'works_at': (r'I work at {value}', r'I am employed by {value}'),
    'worked_at': (r'I worked at {value}',),
    'works_on': (r'I work on {value}', r'I am working on {value} at work',
                 r"I['’]m working on {value} at work", r'My work project is {value}'),
    'role_is': (r'My role is {value}',),
    'certified_in': (r'I am certified in {value}', r"I['’]m certified in {value}"),
    'studied_at': (r'I studied at {value}',),
    'skilled_in': (r'I am skilled in {value}', r"I['’]m skilled in {value}"),
    'prefers': (r'I prefer {value}',),
    'member_of': (r'I am a member of {value}', r"I['’]m a member of {value}"),
    'lives_in': (r'I live in {value}', r'I currently live in {value}', r'My home is in {value}'),
    'practices': (r'I practice {value}', r'I practise {value}'),
    'training_for': (r'I am training for {value}', r"I['’]m training for {value}"),
}


def explicitly_states_claim(content, predicate, value):
    if not all(type(item) is str for item in (content, predicate, value)):
        return False
    if not value or len(content) > 4000 or content != content.strip():
        return False
    # No punctuation or clauses can be smuggled through the object slot.
    from .fact_contract import atomic_label_syntax
    try:
        atomic_label_syntax(value)
    except ValueError:
        return False
    if re.search(r'\b(?:not|never|neither|either|or|maybe|perhaps|if|unless)\b', value, re.I):
        return False
    for form in _FORMS.get(predicate, ()):
        before, after = form.split('{value}')
        pattern = '(?i:' + before + ')' + re.escape(value) + '(?i:' + after + r')[.!]?'
        if re.fullmatch(pattern, content) is not None:
            return True
    return False
