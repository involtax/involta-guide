"""Temporary read-only diagnosis of the production reprocess action."""
import json
import re
import subprocess


def run(*args, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True, timeout=30)


def main():
    docker = ['docker', '--host=unix:///var/run/docker.sock']
    containers = run(*docker, 'ps', '--filter', 'label=com.involta.guide.owner=production', '--format', '{{.Names}}').stdout.splitlines()
    result = {}
    for name in containers:
        if name.endswith(('-web-1', '-voice-1', '-voice-worker-1')):
            logs = run(*docker, 'logs', '--since', '60m', '--tail', '500', name)
            lines = (logs.stdout + logs.stderr).splitlines()
            result[name] = [line[:700] for line in lines if re.search(r'Traceback|Error:|Exception:|File "|raise |KeyError|ProgrammingError|IntegrityError|POST .*reprocess', line)][-65:]
    web = next((n for n in containers if n.endswith('-web-1')), None)
    if web:
        program = '''
import json
from sqlalchemy import text
from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
engine=create_database(Settings())
with engine.connect() as conn:
    rows=conn.execute(text("SELECT l.slug,r.number,r.status,r.content_epoch,l.content_epoch current_epoch FROM reviews r JOIN locations l ON l.id=r.location_id WHERE r.status<>'deleted' ORDER BY r.created_at DESC LIMIT 12")).mappings().all()
    print(json.dumps([dict(row) for row in rows],default=str))
'''
        data = run(*docker, 'exec', '-i', web, 'python', '-', input=program)
        result['recent_review_states'] = json.loads(data.stdout) if data.returncode == 0 else 'query failed'
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
