"""Standalone HTML research-funding report. No network or browser storage required."""
from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .countdown import countdown_html, COUNTDOWN_CSS, COUNTDOWN_JS
from .program_groups import group_records

KST = ZoneInfo('Asia/Seoul')
STATUS = {'open': '접수 중', 'upcoming': '접수 예정', 'closed': '접수 마감', 'unknown': '접수 확인 필요'}
ELIGIBILITY = {'eligible': '추출 조건 충족', 'ineligible': '조건 미충족', 'needs_review': '자격 확인 필요'}
CONDITION = {'met': '충족', 'not_met': '미충족', 'needs_review': '확인 필요'}
KINDS = {'application_start': '접수 시작', 'researcher_deadline': '연구자 제출 마감', 'institution_deadline': '기관승인 마감', 'result_expected': '결과 발표 예정', 'guidelines_release': '요강 공개 예정', 'followup_deadline': '후속 서류 마감'}
PROGRESS = {'interested': '관심', 'reviewing': '검토', 'preparing': '준비', 'submitted': '제출', 'institution_approved': '기관승인', 'awaiting_result': '결과 대기', 'shortlisted': '서면 통과', 'selected': '선정', 'rejected': '미선정', 'on_hold': '보류', 'hold': '보류'}


def _e(value):
    return escape(str(value if value is not None else ''), quote=True)


def _link(label, url, class_name=''):
    address = str(url or '').strip()
    try:
        parsed = urlsplit(address)
        valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        return f'<span>{_e(label)}</span>'
    return f'<a class="{_e(class_name)}" href="{_e(address)}" target="_blank" rel="noopener noreferrer">{_e(label)}<span aria-hidden="true"> ↗</span></a>'


def _date(event):
    value, precision = event.get('value'), event.get('precision', 'unknown')
    if not value:
        return '<span class="muted">미기재·확인 필요</span>'
    if precision == 'datetime':
        try:
            if 'T' not in str(value) and not (' ' in str(value) and ':' in str(value)):
                raise ValueError('time missing')
            point = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if point.tzinfo is None:
                point = point.replace(tzinfo=ZoneInfo(event.get('timezone') or 'Asia/Seoul'))
            return f'<span class="date">{point.astimezone(KST).strftime("%Y.%m.%d %H:%M")}</span><span class="date-unit">한국시간</span>'
        except (ValueError, TypeError, KeyError):
            return f'{_e(value)} <span class="date-unit">시각 확인 필요</span>'
    unit = {'date': '시각 미기재', 'month': '월 단위 예정', 'week': '주차 단위 예정', 'relative': '상대적 예정', 'unknown': '원문 정밀도 확인'}.get(precision, '원문 표기')
    return f'<span class="date">{_e(value)}</span><span class="date-unit">{unit}</span>'


def _due(value):
    if not value:
        return '<span class="muted">기한 확인 필요</span>'
    return _date({'value': value, 'precision': 'datetime' if 'T' in str(value) else 'date', 'timezone': 'Asia/Seoul'})


def _remaining(event):
    if event.get('source_conflict'):
        return countdown_html(None, fallback='마감 원문 확인 필요')
    return countdown_html(event.get('value'), event.get('precision', 'unknown'), event.get('timezone'))


def _list(items, class_name=''):
    return '<ul class="' + class_name + '">' + ''.join(f'<li>{_e(item)}</li>' for item in items) + '</ul>' if items else '<p class="muted small">추가 내용이 없습니다.</p>'


def _empty(text):
    return f'<p class="empty">{_e(text)}</p>'


def _table(headers, rows, *, css_class=''):
    if not rows:
        return _empty('표시할 항목이 없습니다.')
    return f'<div class="table-wrap {css_class}" tabindex="0" aria-label="가로로 스크롤할 수 있는 표"><table><thead><tr>' + ''.join(f'<th scope="col">{_e(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


def _row(cells, notice_id=None):
    attribute = f' data-filter-id="{_e(notice_id)}"' if notice_id is not None else ''
    return f'<tr{attribute}>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>'


def render_html_report(notices: list[dict], evaluations: list[dict], profile: dict,
                       as_of: datetime, changes: list[dict], collection_status: list[dict],
                       progress: dict | None = None, *, summary: dict | None = None,
                       seasons: dict | None = None, discovery: dict | None = None,
                       catalog: dict | None = None, application_history: list | None = None,
                       program_groups: dict | None = None) -> str:
    progress, summary, seasons, discovery = progress or {}, summary or {}, seasons or {}, discovery or {}
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=KST)
    by_id = {str(n.get('id')): n for n in notices}
    lookup = {str(e.get('notice_id')): e for e in evaluations}
    completed = set(progress.get('completed_tasks') or [])
    tasks = [t for e in evaluations for t in e.get('tasks', []) if t.get('id') not in completed]
    order = {'open': 0, 'upcoming': 1, 'unknown': 2, 'closed': 3}
    ordered = sorted(notices, key=lambda n: (order.get(lookup.get(str(n.get('id')), {}).get('status'), 2), str(n.get('title', ''))))
    sponsors = sorted({str(n.get('sponsor') or '지원기관 미확인') for n in notices})
    stats = [('분석한 공고·회차', len(notices)), ('접수 중', sum(e.get('status') == 'open' for e in evaluations)), ('접수 예정', sum(e.get('status') == 'upcoming' for e in evaluations)), ('남은 할 일', len(tasks))]
    notice_groups = group_records(ordered, 'notice', program_groups)
    parts = []
    for group in notice_groups:
        tracks, comparison_rows = [], []
        group_sponsors = list(dict.fromkeys(n.get('sponsor') or '지원기관 미확인' for n in group['items']))
        for n in group['items']:
            identifier = str(n.get('id'))
            ev = lookup.get(identifier, {})
            state = ev.get('status', 'unknown')
            events = n.get('events', [])
            deadline = next((x for x in events if x.get('kind') == 'researcher_deadline'), {})
            approval = next((x for x in events if x.get('kind') == 'institution_deadline'), {})
            track_label = group.get('track_labels', {}).get(identifier) or n.get('track') or n.get('title') or identifier
            condition_rows = [_row([f'<span class="condition { _e(c.get("status", "needs_review"))}">{_e(CONDITION.get(c.get("status"), "확인 필요"))}</span>', '<strong>'+_e(c.get('label'))+'</strong><div class="muted small">'+_e(c.get('reason'))+'</div>', '<blockquote>'+_e(c.get('evidence_quote') or '원문 인용 미첨부')+'</blockquote>']) for c in ev.get('conditions', [])]
            state_value = (progress.get('applications') or {}).get(identifier, n.get('application_status'))
            if isinstance(state_value, dict):
                state_value = state_value.get('status')
            state_text = PROGRESS.get(state_value, '진행상태 미등록')
            evidence = {'live': '웹 수집 원문', 'official_snapshot': '공식 원문 저장본'}.get(n.get('evidence_status'), '근거 상태 확인 필요')
            missing = list(dict.fromkeys(n.get('missing_information', []) + ev.get('warnings', [])))
            keywords = ev.get('topic_matches') or []
            search = ' '.join(str(x) for x in [group['title'], track_label, n.get('title', ''), n.get('sponsor', ''), n.get('track', ''), *n.get('keywords', []), *n.get('required_documents', [])])
            eligibility = '<span class="eligibility '+_e(ev.get('eligibility', 'needs_review'))+'">'+_e(ELIGIBILITY.get(ev.get('eligibility'), '자격 확인 필요'))+'</span>'
            comparison_rows.append(_row(['<strong>'+_e(track_label)+'</strong>', _date(deadline)+_remaining(deadline), _date(approval)+_remaining(approval), eligibility], identifier))
            details = f'<div class="detail-content"><h4>조건별 비교</h4>{_table(["판정", "조건과 비교", "원문 근거"], condition_rows)}<h4>제출서류</h4>{_list(n.get("required_documents", []))}<h4>추가 확인</h4>{_list(missing)}<h4>공고 메모</h4>{_list(n.get("notes", []))}<p class="small muted">공고 식별자: {_e(identifier)}</p></div>'
            tracks.append(f'''<details class="notice-track" id="notice-{_e(identifier)}" data-card data-filter-id="{_e(identifier)}" data-status="{_e(state)}" data-sponsor="{_e(n.get('sponsor') or '지원기관 미확인')}" data-search="{_e(search)}">
<summary><strong>{_e(track_label)}</strong><span class="badge {_e(state)}">{_e(STATUS.get(state, '확인 필요'))}</span><span class="track-open-label">자격·서류 보기</span></summary>
<div class="notice-track-body"><p class="notice-track-title">{_e(n.get('title') or identifier)}</p><div class="card-meta"><span>{_e(n.get('year') or '사업연도 미확인')}</span><span>{_e(evidence)}</span></div>
<div class="deadline-grid"><div><span class="field-label">연구자 제출 마감</span>{_date(deadline)}{_remaining(deadline)}</div><div><span class="field-label">기관승인 마감</span>{_date(approval)}{_remaining(approval)}</div></div>
<div class="card-bottom">{eligibility}<span class="muted small">{_e(state_text)}</span>{_link('공식 공고', n.get('source_url'), 'source-link')}</div>
{('<div class="keywords">연구 키워드 일치 · '+_e(', '.join(keywords))+'</div>') if keywords else ''}{details}</div></details>''')
        parts.append('<article class="grant-card notice-group" data-notice-group="'+_e(group['id'])+'"><div class="card-top"><span class="eyebrow">'+_e(' · '.join(group_sponsors))+'</span><span class="badge neutral" data-notice-group-count>'+str(len(group['items']))+'개 전형·회차</span></div><h3>'+_e(group['title'])+'</h3>'+_table(['전형·회차', '연구자 제출 마감', '기관승인 마감', '자격 비교'], comparison_rows, css_class='notice-comparison')+'<div class="notice-tracks">'+''.join(tracks)+'</div></article>')
    cards = ''.join(parts)
    schedule_rows = []
    for n in ordered:
        for event in n.get('events', []):
            nature = '출처 충돌·확인 필요' if event.get('source_conflict') else {'announced': '공식 게시 일정', 'planned': '공식 예정', 'unknown': '확인 필요'}.get(event.get('status'), '확인 필요')
            countdown = _remaining(event) if event.get('kind') in {'researcher_deadline', 'institution_deadline', 'followup_deadline'} else ''
            schedule_rows.append(_row([_e(n.get('title')), _e(event.get('label') or KINDS.get(event.get('kind'), '일정')), _date(event) + countdown, '<span class="nature">'+_e(nature)+'</span>', _link('원문 보기', event.get('source_url') or n.get('source_url'))], str(n.get('id'))))
        internal = (profile.get('internal_deadlines') or {}).get(str(n.get('id')))
        if internal:
            schedule_rows.append(_row([_e(n.get('title')), '대학 내부마감', _due(internal), '사용자 등록', '소속기관 확인 정보'], str(n.get('id'))))
    task_rows = []
    for task in sorted(tasks, key=lambda t: (not bool(t.get('due')), str(t.get('due') or ''), str(t.get('notice_id') or ''))):
        n = by_id.get(str(task.get('notice_id')), {})
        kind = {'official': '공식 절차', 'preparation': '준비 작업', 'verification': '확인할 일'}.get(task.get('kind'), '확인할 일')
        task_rows.append(_row(['<strong>'+_e(task.get('title'))+'</strong><div class="muted small">'+_e(task.get('reason'))+'</div>', _e(n.get('title') or task.get('notice_id')), _due(task.get('due')), _e(kind), '<code>'+_e(task.get('id'))+'</code>'], str(task.get('notice_id'))))
    season_rows = [_row([_link(x.get('program'), x.get('source_url')), _e(x.get('timing')), _e(x.get('evidence_type')), _e(x.get('action'))]) for x in seasons.get('items', [])]
    changes_html = []
    field_names = {'title': '공고명', 'events': '일정', 'eligibility': '신청자격', 'required_documents': '제출서류', 'missing_information': '확인할 정보'}
    for change in changes:
        kind = {'new': '신규', 'added': '신규', 'updated': '수정', 'changed': '수정'}.get(change.get('kind'), '변경')
        fields = ', '.join(field_names.get(x, x) for x in change.get('fields', []))
        changes_html.append('<li><span class="change-dot"></span><div><strong>'+_e(kind)+' · '+_e(change.get('title') or change.get('notice_id'))+'</strong><p class="muted small">'+_e('변경 항목: '+fields if fields else '이번 비교에서 새로 확인한 공고입니다.')+'</p></div></li>')
    source_rows = []
    source_names = {'msit': '과학기술정보통신부', 'samsung_science': '삼성 Science', 'amazon': 'Amazon Research Awards', 'posco': '포스코청암재단', 'demo': '준비된 검토자료'}
    for record in collection_status:
        state = {'ok': '수집 성공', 'error': '수집 실패', 'snapshot': '공식 저장본', 'cached': '수집 캐시', 'disabled': '자동수집 미사용'}.get(record.get('status'), '확인 필요')
        name = record.get('name') or source_names.get(record.get('source_id'), record.get('source_id') or '출처')
        fetched = record.get('fetched_at') or record.get('checked_at')
        succeeded = record.get('last_success_at') or (fetched if record.get('status') == 'ok' else None)
        note = record.get('error') or record.get('warning') or record.get('note') or ''
        if record.get('cache_used') or record.get('cached') or record.get('from_cache'):
            note = (str(note)+' · ' if note else '')+'저장 캐시 사용'
        source_rows.append(_row([_link(name, record.get('source_url') or record.get('url')), '<span class="badge '+('error' if record.get('status') == 'error' else 'neutral')+'">'+_e(state)+'</span>', _e(fetched or '미확인'), _e(succeeded or '미확인'), _e(note or '—')]))
    discovery_rows = []
    for candidate in discovery.get('candidates', []):
        title = str(candidate.get('title') or '제목 미확인')
        if '선정결과' in title:
            kind = '결과 공지'
        elif '장학' in title:
            kind = '장학·대상 확인'
        elif any(x in title for x in ('공모전', '수상', '지정계획')):
            kind = '시상·지정 등'
        elif any(x in title for x in ('수정', '정정', '연장')):
            kind = '수정·정정 후보'
        else:
            kind = '연구지원 후보'
        discovery_rows.append(_row([_link(title, candidate.get('url')), _e(kind), _e(source_names.get(candidate.get('source_id'), candidate.get('source_id') or '출처'))]))
    catalog_html, catalog_css, catalog_js = '', '', ''
    if catalog and catalog.get('profile_id') == profile.get('id'):
        from .catalog_html import render_catalog, CATALOG_CSS, CATALOG_JS
        catalog_html, catalog_css, catalog_js = render_catalog(catalog, profile, program_groups), CATALOG_CSS, CATALOG_JS
        counts = catalog.get('counts', {})
        catalog_programs = group_records(catalog.get('candidates', []), 'candidate', program_groups if program_groups is not None else catalog.get('program_groups'))
        current_programs = sum(any(item['availability'] in {'open','rolling','announced'} and item['fit'] != 'excluded' for item in group['items']) for group in catalog_programs)
        stats = [('지원사업', len(catalog_programs)), ('세부 전형', counts.get('total', 0)),
                 ('모집·일정 확인 사업', current_programs), ('현재 제외 전형', counts.get('excluded', 0))]

    mode = {'demo':'준비된 검토자료 · 모델 실행 없음','cv_review':'CV·공식 지원기관 자료를 대조한 맞춤 검토'}.get(summary.get('mode'), '로컬 모델로 분석한 자료')
    profile_tag = '<span class="profile-tag">수업용 가상 프로필</span>' if profile.get('is_demo') else ''
    errors = summary.get('errors') or []
    error_html = '<div class="notice error-note" role="status">분석하지 못한 공고 '+str(len(errors))+'건: '+_e('; '.join(str(x.get('notice_id'))+': '+str(x.get('error')) for x in errors))+'</div>' if errors else ''
    notes_html = '<p>진행상태만 갱신한 보고서입니다. 이번 갱신에서는 웹 조회·모델 추론을 실행하지 않았습니다.</p>' if summary.get('progress_only') else ('<p>저장된 분석 결과를 HTML로 정리했습니다. 새 웹 조회·모델 추론을 실행하지 않았습니다.</p>' if summary.get('report_only') else '')
    generated = summary.get('created_at') or as_of.isoformat()
    stats_html = ''.join('<div class="stat"><span>'+_e(label)+'</span><strong>'+str(count)+'</strong></div>' for label, count in stats)
    sponsor_options = ''.join('<option value="'+_e(s)+'">'+_e(s)+'</option>' for s in sponsors)
    change_content = '<ul class="changes">'+''.join(changes_html)+'</ul>' if changes_html else _empty('이번 비교에서 새 변경사항이 없습니다.')
    template = '''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light"><title>Grant Finder · 연구지원 현황</title><style>__CSS__</style></head>
<body><a class="skip" href="#main">본문 바로가기</a><div class="app-shell"><aside class="sidebar"><a class="brand" href="#overview"><span class="brand-mark" aria-hidden="true">G</span><span>Grant<br>Finder</span></a><p class="sidebar-caption">연구지원 보조 에이전트</p><nav aria-label="보고서 목차"><a href="#overview">개요</a><a href="#grants">지원 후보</a><a href="#calendar">일정</a><a href="#tasks">할 일</a><a href="#seasons">모집 시기</a><a href="#sources">공식 출처</a></nav><div class="sidebar-foot">연구비 기회부터<br>신청 준비까지</div></aside>
<main id="main"><header class="page-header" id="overview"><div><p class="eyebrow">RESEARCH FUNDING BRIEF</p><h1>연구지원 현황</h1><p class="subtitle">__NAME__ __PROFILE_TAG__</p></div><button class="button print-button" type="button" id="print-button">인쇄 / PDF</button></header><div class="report-context"><span><span class="context-dot"></span>__MODE__</span><span>판단 기준 __AS_OF__</span></div>__ERRORS__<div class="stats">__STATS__</div>
<section id="grants" aria-labelledby="grants-title"><div class="section-heading"><div><p class="eyebrow">OPPORTUNITIES</p><h2 id="grants-title">지원 후보</h2></div><span class="muted small" id="filter-count" aria-live="polite">전체 __TOTAL__</span></div><p class="section-description">같은 사업의 전형·회차를 묶었습니다. 전형별 마감과 자격을 비교하고 세부 내용을 펼쳐 확인합니다.</p><form class="filters" id="filters" role="search"><label class="search-field"><span>공고 검색</span><input id="search" type="search" placeholder="사업명, 기관, 연구 키워드" autocomplete="off"></label><label><span>접수 상태</span><select id="status-filter"><option value="">전체 상태</option><option value="open">접수 중</option><option value="upcoming">접수 예정</option><option value="closed">접수 마감</option><option value="unknown">확인 필요</option></select></label><label class="sponsor-field"><span>지원기관</span><select id="sponsor-filter"><option value="">전체 기관</option>__SPONSORS__</select></label><button type="reset" class="button reset-button">초기화</button></form><div class="grant-grid">__CARDS__</div><p class="empty filter-empty" data-empty="cards" hidden>검색 조건에 맞는 공고가 없습니다.</p></section>
<section id="calendar" aria-labelledby="calendar-title"><div class="section-heading"><div><p class="eyebrow">CALENDAR</p><h2 id="calendar-title">신청과 결과 일정</h2></div><span class="section-tag">시각은 한국시간</span></div><p class="section-description">월·주차·상대적 예정은 원문 정밀도로 표시합니다. 예정일이 지났다는 이유로 선정 여부를 판단하지 않습니다.</p>__CALENDAR__<p class="empty filter-empty" data-empty="calendar" hidden>현재 필터에 해당하는 일정이 없습니다.</p></section>
<section id="tasks" aria-labelledby="tasks-title"><div class="section-heading"><div><p class="eyebrow">NEXT ACTIONS</p><h2 id="tasks-title">이번에 할 일</h2></div><span class="section-tag">미완료 __TASK_COUNT__건</span></div><p class="section-description">공식 절차, 준비 작업, 확인할 일을 구분했습니다. 완료로 기록한 항목은 제외했습니다.</p>__TASKS__<p class="empty filter-empty" data-empty="tasks" hidden>현재 필터에 해당하는 미완료 할 일이 없습니다.</p><p class="footnote">이 화면은 저장된 보고서입니다. 진행상태 기록은 실행 프로그램에서 관리합니다.</p></section>
<section id="seasons" aria-labelledby="seasons-title"><div class="section-heading"><div><p class="eyebrow">LOOK AHEAD</p><h2 id="seasons-title">공고 예정·통상 모집 시기</h2></div></div><p class="section-description">공식 계획, 기관의 정기 안내, 과거 회차 이력을 구분합니다. 참고 시기는 확정 접수 마감이 아닙니다.</p>__SEASONS__</section>
<section id="changes" aria-labelledby="changes-title"><details class="panel"><summary><span><span class="eyebrow">UPDATES</span><strong id="changes-title">신규·변경사항</strong></span><span class="muted small">__CHANGE_COUNT__건</span></summary><div class="panel-body">__CHANGES__</div></details></section>
<section id="sources" aria-labelledby="sources-title"><div class="section-heading"><div><p class="eyebrow">EVIDENCE</p><h2 id="sources-title">공식 출처 확인 기록</h2></div></div><p class="section-description">웹 재수집과 공식 저장본을 구분합니다. 수집 실패는 ‘공고 없음’을 뜻하지 않습니다.</p>__SOURCES__</section>
<section id="discovery"><details class="panel"><summary><span><span class="eyebrow">DISCOVERY</span><strong>목록에서 발견한 공고</strong></span><span class="muted small">__DISCOVERY_COUNT__건</span></summary><div class="panel-body"><p class="section-description">아래는 목록에서 발견한 후보입니다. 지원자격과 최종 마감을 분석한 지원 후보와 구분합니다.</p><p class="muted small">목록 탐색 기록: __DISCOVERED_AT__</p>__DISCOVERY__</div></details></section>
<details class="panel technical"><summary>보고서 생성 정보</summary><div class="panel-body"><dl><div><dt>생성 시각</dt><dd>__GENERATED__</dd></div><div><dt>최근 분석 실행의 신규 추론</dt><dd>__MODEL_CALLS__건</dd></div><div><dt>최근 분석 실행의 캐시 재사용</dt><dd>__CACHE_HITS__건</dd></div></dl>__PROGRESS_NOTE__<p class="muted small">보고서를 열어 보는 동안 웹 수집이나 모델 추론을 추가 실행하지 않습니다.</p></div></details><footer>Grant Finder <span>공식 근거와 함께 준비하는 연구지원</span></footer></main></div><script>__JS__</script></body></html>'''
    if application_history is not None:
        from .application_history import render_history_html
        application_history = [item for item in application_history if profile.get('id') and item.get('profile_id') == profile['id']]
        template = template.replace('<a href="#seasons">', '<a href="#application-history">지원 이력</a><a href="#seasons">', 1)
        template = template.replace('<section id="seasons"', render_history_html(application_history) + '\n<section id="seasons"', 1)
    if catalog_html:
        template = template.replace('<section id="grants"', '__CATALOG__<section id="grants"', 1)
        template = template.replace('<a href="#grants">지원 후보</a>', '<a href="#funding-catalog">전체 후보</a><a href="#grants">상세 추적</a>')
        template = template.replace('<h2 id="grants-title">지원 후보</h2>', '<h2 id="grants-title">상세 추적 중인 공고</h2>')
    template = template.replace('<div class="stats">', '<p class="countdown-context">남은 시간은 현재 시각 기준 · <span id="countdown-clock"></span> 한국시간 · 1분마다 자동 갱신</p><div class="stats">', 1)
    replacements = {'__CSS__': CSS + catalog_css + COUNTDOWN_CSS, '__JS__': JS + catalog_js + COUNTDOWN_JS, '__NAME__': _e(profile.get('name') or '연구자 미등록'), '__PROFILE_TAG__': profile_tag, '__MODE__': _e(mode), '__AS_OF__': as_of.astimezone(KST).strftime('%Y.%m.%d' if summary.get('mode') == 'cv_review' else '%Y.%m.%d %H:%M'), '__ERRORS__': error_html, '__STATS__': stats_html, '__TOTAL__': str(len(notice_groups))+'개 사업 · '+str(len(notices))+'개 전형', '__SPONSORS__': sponsor_options, '__CARDS__': cards or _empty('아직 분석한 공고가 없습니다.'), '__CALENDAR__': _table(['공고', '단계', '일정', '성격', '근거'], schedule_rows, css_class='schedule-table'), '__TASKS__': _table(['할 일과 이유', '공고', '기한', '성격', '식별자'], task_rows, css_class='tasks-table'), '__TASK_COUNT__': str(len(tasks)), '__SEASONS__': _table(['사업', '공고·접수 시기', '근거 성격', '확인할 일'], season_rows), '__CHANGES__': change_content, '__CHANGE_COUNT__': str(len(changes)), '__SOURCES__': _table(['출처', '상태', '이번 조회·저장', '마지막 성공', '설명'], source_rows), '__DISCOVERY__': _table(['발견한 제목', '제목으로 본 유형', '출처'], discovery_rows), '__DISCOVERY_COUNT__': str(len(discovery_rows)), '__DISCOVERED_AT__': _e(discovery.get('checked_at') or '탐색 기록 없음'), '__GENERATED__': _e(generated), '__MODEL_CALLS__': str(summary.get('model_calls', 0)), '__CACHE_HITS__': str(summary.get('cache_hits', 0)), '__PROGRESS_NOTE__': notes_html, '__CATALOG__': catalog_html}
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


CSS = r'''
:root{--bg:#f5f5f0;--paper:#fff;--ink:#172b3a;--muted:#64727a;--line:#dfe5e2;--teal:#12685d;--teal-soft:#e5f3ed;--blue:#315a9f;--blue-soft:#eaf0fb;--amber:#926425;--amber-soft:#fbf1dc;--radius:14px}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;font-size:15px;line-height:1.65}a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline}button,input,select{font:inherit}button,select{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:3px solid #66a8d4;outline-offset:4px}[hidden]{display:none!important}.skip{position:fixed;top:-60px;left:20px;z-index:20;background:var(--ink);color:#fff;padding:12px}.skip:focus{top:12px}.app-shell{max-width:1600px;margin:auto;display:grid;grid-template-columns:190px minmax(0,1fr)}.sidebar{padding:45px 28px;height:100vh;position:sticky;top:0;border-right:1px solid var(--line);display:flex;flex-direction:column}.brand{display:flex;gap:12px;align-items:center;color:var(--ink);font-size:19px;font-weight:760;line-height:1.13;letter-spacing:-.4px}.brand:hover{text-decoration:none}.brand-mark{display:grid;place-items:center;width:36px;height:42px;color:#fff;background:var(--teal);border-radius:11px 11px 11px 2px;font-size:24px}.sidebar-caption{font-size:11px;color:var(--muted);margin:22px 0 36px;white-space:nowrap}.sidebar nav{display:grid;gap:9px}.sidebar nav a{padding:9px 10px;color:var(--muted);border-radius:8px;font-size:14px}.sidebar nav a:hover{background:#e8ece6;color:var(--teal);text-decoration:none}.sidebar-foot{margin-top:auto;color:#8b969a;font-size:11px;line-height:1.8}main{min-width:0;padding:44px 48px 26px;max-width:1320px}.page-header{display:flex;justify-content:space-between;align-items:center;gap:18px}.eyebrow{margin:0;color:var(--teal);font-size:10px;font-weight:750;letter-spacing:1.6px}.page-header h1{margin:8px 0 10px;font-size:34px;line-height:1.2;letter-spacing:-1.5px}.subtitle{margin:0;color:var(--muted);font-size:14px}.profile-tag{display:inline-block;padding:2px 8px;margin-left:8px;border:1px solid #d9dfd6;border-radius:5px;font-size:11px;color:#6f7b68;vertical-align:middle}.button{border:1px solid #cbd5d2;border-radius:8px;padding:9px 14px;color:var(--ink);background:transparent;font-size:12px;font-weight:600;white-space:nowrap}.button:hover{background:#e8ece6}.report-context{display:flex;gap:18px;flex-wrap:wrap;align-items:center;margin-top:22px;padding-top:17px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.context-dot{display:inline-block;background:var(--teal);width:6px;height:6px;border-radius:50%;margin-right:6px}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:25px 0 38px}.stat{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:19px 22px;display:flex;align-items:center;justify-content:space-between;gap:8px}.stat span{font-size:12px;color:var(--muted)}.stat strong{font-size:30px;font-weight:680;letter-spacing:-1px;line-height:1}.stat:nth-child(2) strong{color:var(--teal)}.stat:nth-child(3) strong{color:var(--blue)}section{margin-top:34px}.section-heading{display:flex;justify-content:space-between;align-items:end;gap:14px;margin-bottom:11px}.section-heading h2{font-size:22px;line-height:1.3;letter-spacing:-.8px;margin:5px 0 0;font-weight:720}.section-description{font-size:12px;color:var(--muted);margin:0 0 18px;line-height:1.8}.section-tag{font-size:11px;color:var(--muted);white-space:nowrap}.small{font-size:11px}.muted{color:var(--muted)}.filters{display:grid;grid-template-columns:minmax(180px,1fr) 135px minmax(140px,200px) auto;gap:12px;margin:19px 0 18px;align-items:end}.filters label{display:grid;gap:6px;min-width:0}.filters label>span{font-size:10px;font-weight:650;color:var(--muted)}input,select{width:100%;min-width:0;border:1px solid #cfd8d3;background:#fff;border-radius:8px;color:var(--ink);padding:10px 12px;height:43px;font-size:12px}.reset-button{height:43px}.grant-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}.grant-card{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:22px;min-width:0;transition:border-color .15s}.grant-card:hover{border-color:#bccfc6}.card-top{display:flex;justify-content:space-between;align-items:start;gap:10px}.card-top .eyebrow{font-size:10px;letter-spacing:0;max-width:72%;line-height:1.6;color:var(--muted)}.badge{display:inline-block;white-space:nowrap;font-size:10px;line-height:1.4;border-radius:5px;padding:5px 8px;font-weight:650}.badge.open{background:var(--teal-soft);color:var(--teal)}.badge.upcoming{background:var(--blue-soft);color:var(--blue)}.badge.closed,.badge.neutral{background:#eef0ed;color:#68736a}.badge.unknown{background:var(--amber-soft);color:var(--amber)}.badge.error{background:#fbe7e4;color:#ac453a}.grant-card h3{font-size:18px;letter-spacing:-.6px;line-height:1.55;margin:12px 0 10px;overflow-wrap:anywhere;min-height:54px}.card-meta{display:flex;gap:8px;font-size:10px;color:var(--muted)}.card-meta span+span:before{content:'·';margin-right:8px}.deadline-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;background:#f7f9f6;margin:17px 0;padding:13px 14px;border-radius:8px}.deadline-grid>div{min-width:0}.deadline-grid>div+div{border-left:1px solid var(--line);padding-left:13px}.field-label{display:block;font-size:10px;color:var(--muted);margin-bottom:5px}.date{font-size:12px;line-height:1.7;font-weight:650;display:block;overflow-wrap:anywhere}.date-unit{display:block;font-size:9px;color:var(--muted);margin-top:2px}.card-bottom{display:flex;flex-wrap:wrap;align-items:center;gap:7px}.eligibility{font-size:10px;font-weight:650;color:var(--amber)}.eligibility.eligible{color:var(--teal)}.eligibility.ineligible{color:#9f4e43}.source-link{margin-left:auto;font-size:11px;font-weight:650;white-space:nowrap}.keywords{font-size:10px;color:var(--teal);margin-top:10px}.card-details{border-top:1px solid var(--line);margin-top:15px;padding-top:12px}.card-details summary{cursor:pointer;font-size:11px;font-weight:600;color:var(--muted);list-style-position:inside}.card-details[open] summary{color:var(--teal)}.detail-content{margin-top:18px;font-size:12px}.detail-content h4{font-size:12px;margin:18px 0 8px}.detail-content ul{padding-left:17px;margin:6px 0}.detail-content li{margin:7px 0;overflow-wrap:anywhere}.detail-content .table-wrap{max-height:360px;border-radius:7px}.detail-content th,.detail-content td{font-size:10px;padding:9px 10px}.detail-content table{min-width:520px}.condition{white-space:nowrap;font-size:10px}.condition.met{color:var(--teal)}.condition.not_met{color:#a34c40}.condition.needs_review{color:var(--amber)}blockquote{margin:0;color:var(--muted);overflow-wrap:anywhere}.table-wrap{border:1px solid var(--line);border-radius:12px;overflow:auto;background:var(--paper);max-height:510px}.table-wrap table{width:100%;border-collapse:collapse;text-align:left;min-width:680px}th{background:#f0f4ee;color:#5b6a63;font-size:10px;font-weight:650;padding:13px 16px;position:sticky;top:0;z-index:1;white-space:nowrap}td{font-size:11px;vertical-align:top;padding:14px 16px;border-top:1px solid #edf0eb;overflow-wrap:anywhere;line-height:1.7}td .small{margin-top:5px}.schedule-table td:first-child,.tasks-table td:nth-child(2){min-width:170px;max-width:260px}.schedule-table td:nth-child(3){min-width:170px}.tasks-table td:first-child{min-width:250px;max-width:440px}.tasks-table td:nth-child(3){min-width:150px}.tasks-table td:last-child code{font-size:9px;color:#7e8a83}.nature{font-size:10px;color:var(--muted);white-space:nowrap}.footnote{font-size:10px;color:var(--muted);margin:9px 0 0}.empty{border:1px dashed #cdd7cf;border-radius:10px;padding:25px;text-align:center;font-size:12px;color:var(--muted);background:#f8faf6}.filter-empty{margin:15px 0}.panel{border:1px solid var(--line);border-radius:12px;background:var(--paper);overflow:hidden}.panel>summary{padding:18px 20px;cursor:pointer;display:flex;justify-content:space-between;gap:20px;align-items:center;list-style:none}.panel>summary::-webkit-details-marker{display:none}.panel>summary>span:first-child{display:grid;gap:4px}.panel>summary strong{font-size:15px;letter-spacing:-.3px}.panel>summary:after{content:'+';font-size:20px;line-height:1;color:var(--muted)}.panel[open]>summary:after{content:'−'}.panel-body{padding:0 20px 20px}.panel-body>.table-wrap{margin-top:14px}.changes{list-style:none;margin:0;padding:0}.changes li{display:flex;gap:12px;padding:12px 0;border-top:1px solid #edf0eb;font-size:12px}.changes p{margin:4px 0 0}.change-dot{width:6px;height:6px;border-radius:50%;background:var(--teal);margin-top:8px;flex-shrink:0}.technical{margin-top:30px;color:var(--muted);background:transparent}.technical>summary{font-size:11px;padding:13px 17px}.technical .panel-body{font-size:11px}.technical dl{margin:0;display:grid;gap:7px}.technical dl>div{display:flex;gap:18px;flex-wrap:wrap}.technical dt{min-width:140px}.technical dd{margin:0}.notice{font-size:12px;padding:13px 16px;border-radius:9px;margin-top:20px}.error-note{background:#fff0ed;color:#9d473d;border:1px solid #efc6bc}footer{border-top:1px solid var(--line);margin-top:32px;padding-top:18px;display:flex;justify-content:space-between;color:#6f8078;font-size:10px;letter-spacing:.2px}footer span{color:#8a9690}
@media(min-width:1400px){main{padding-left:60px;padding-right:60px}.grant-card{padding:25px}}@media(max-width:1120px){.app-shell{grid-template-columns:150px minmax(0,1fr)}.sidebar{padding:35px 18px}main{padding:35px 28px}.stat{padding:17px 14px}.stat strong{font-size:26px}.filters{grid-template-columns:minmax(150px,1fr) 125px 170px auto}.grant-card{padding:18px}.deadline-grid{grid-template-columns:1fr;gap:8px}.deadline-grid>div+div{border-left:0;border-top:1px solid var(--line);padding-left:0;padding-top:8px}.grant-card h3{font-size:17px}}@media(max-width:850px){.app-shell{display:block}.sidebar{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line);padding:19px 24px;display:flex;flex-direction:row;align-items:center;gap:26px}.brand{font-size:14px;line-height:1.1}.brand br{display:none}.brand span:last-child{white-space:nowrap}.brand-mark{width:29px;height:31px;font-size:19px}.sidebar nav{display:flex;gap:4px;overflow:auto;margin-left:auto}.sidebar nav a{white-space:nowrap;padding:6px 8px;font-size:11px}.sidebar-caption,.sidebar-foot{display:none}main{padding:28px 24px}.page-header h1{font-size:29px}.filters{grid-template-columns:1fr 140px 180px}.reset-button{grid-column:3;justify-self:end;height:auto;padding:5px 12px}.stats{gap:10px;margin-bottom:30px}.stat{display:block}.stat strong{display:block;margin-top:10px}.grant-card h3{min-height:0}.deadline-grid{grid-template-columns:1fr 1fr}.deadline-grid>div+div{border-top:0;border-left:1px solid var(--line);padding-top:0;padding-left:10px}.sidebar nav a:first-child{display:none}}@media(max-width:620px){.sidebar{padding:16px 18px;gap:15px;flex-wrap:wrap}.sidebar nav{width:100%;margin:0;padding-top:4px}.sidebar nav a{padding-left:0;padding-right:16px}.sidebar nav a:first-child{display:block}main{padding:24px 18px}.page-header{align-items:start}.page-header h1{font-size:27px}.eyebrow{font-size:9px}.print-button{padding:8px 10px;font-size:10px}.subtitle{font-size:12px}.profile-tag{margin:7px 0 0;display:table;font-size:10px}.report-context{gap:7px;display:grid;font-size:10px;margin-top:18px}.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{display:flex;padding:16px}.stat strong{margin:0;font-size:27px}.stat span{font-size:10px}.filters{grid-template-columns:1fr 1fr;gap:9px}.search-field{grid-column:1/-1}.sponsor-field{grid-column:2}.reset-button{grid-column:1/-1;justify-self:end}.grant-grid{grid-template-columns:1fr;gap:13px}.grant-card{padding:19px}.grant-card h3{font-size:18px}.section-heading h2{font-size:20px}.section-description{font-size:11px}.section-tag{font-size:10px}.table-wrap{max-height:430px}td,th{padding:11px 12px}.panel>summary{padding:16px}.panel-body{padding:0 16px 16px}section{margin-top:30px}footer{gap:12px;display:grid}.technical dl>div{display:grid;gap:2px}}
@media print{body{background:#fff;color:#000;font-size:10pt}.app-shell{display:block}.sidebar,.filters,.print-button,.skip,.filter-empty{display:none!important}main{padding:0;max-width:none}.stats{margin:15px 0;gap:10px}.stat{padding:10px}.grant-grid{grid-template-columns:1fr 1fr;gap:10px}.grant-card{padding:14px;break-inside:avoid}.grant-card h3{font-size:13pt}.deadline-grid{grid-template-columns:1fr 1fr}.table-wrap{overflow:visible;max-height:none;border-radius:0}table{min-width:0!important;width:100%}th{position:static}td,th{font-size:8pt;padding:7px}.tasks-table td,.schedule-table td{min-width:0!important}.card-details:not([open]) .detail-content,.panel:not([open]) .panel-body{display:block}details>summary{list-style:none}section{margin-top:22px}.report-context{font-size:8pt}.technical{display:none}a{color:inherit}.card-details{font-size:8pt}.panel{border:0}.panel-body{padding:0}.panel>summary{padding:10px 0}footer{margin-top:20px}}
'''


CSS += r'''
.notice-group h3{min-height:0;margin:12px 0 16px}.notice-comparison{max-height:none;border-radius:8px}.notice-comparison table{min-width:560px}.notice-comparison th,.notice-comparison td{padding:10px;font-size:10px}.notice-comparison td:first-child{min-width:100px}.notice-comparison .date{font-size:10px}.notice-comparison .deadline-countdown{font-size:10px}.notice-tracks{margin-top:15px;border-top:1px solid var(--line)}.notice-track+.notice-track{border-top:1px solid var(--line)}.notice-track>summary{display:flex;gap:10px;align-items:center;cursor:pointer;padding:14px 0;list-style:none}.notice-track>summary::-webkit-details-marker{display:none}.notice-track>summary:after{content:'+';margin-left:2px;color:var(--teal);font-size:17px}.notice-track[open]>summary:after{content:'−'}.notice-track>summary>strong{font-size:12px;line-height:1.6;flex:1;overflow-wrap:anywhere}.track-open-label{font-size:9px;color:var(--muted)}.notice-track-title{font-size:12px;font-weight:650;margin:0 0 9px}.notice-track-body{padding:0 0 15px}.notice-track .detail-content{margin-top:12px}.notice-group .table-wrap{max-width:100%}
@media(max-width:620px){.notice-track>summary{flex-wrap:wrap;gap:7px}.notice-track>summary>strong{flex-basis:55%}.track-open-label{display:none}.notice-comparison td,.notice-comparison th{padding:9px}.notice-group h3{font-size:17px}}
@media print{.notice-track:not([open])>.notice-track-body{display:block}.notice-track>summary{padding:8px 0}.track-open-label{display:none}}
'''

JS = r'''
(() => {
  'use strict';
  const form = document.getElementById('filters');
  const search = document.getElementById('search');
  const status = document.getElementById('status-filter');
  const sponsor = document.getElementById('sponsor-filter');
  const cards = Array.from(document.querySelectorAll('[data-card]'));
  const parentGroups = Array.from(document.querySelectorAll('[data-notice-group]'));
  const elements = Array.from(document.querySelectorAll('[data-filter-id]'));
  function update() {
    const term = search.value.trim().toLocaleLowerCase();
    const visible = new Set();
    cards.forEach(card => {
      const matches = (!term || card.dataset.search.toLocaleLowerCase().includes(term)) && (!status.value || card.dataset.status === status.value) && (!sponsor.value || card.dataset.sponsor === sponsor.value);
      if (matches) visible.add(card.dataset.filterId);
    });
    elements.forEach(element => { element.hidden = !visible.has(element.dataset.filterId); });
    let visibleGroups = 0;
    parentGroups.forEach(group => {
      const count = Array.from(group.querySelectorAll('[data-card]')).filter(card => !card.hidden).length;
      group.hidden = count === 0;
      if (count) visibleGroups++;
      const label = group.querySelector('[data-notice-group-count]');
      if (label) label.textContent = count + '개 전형·회차';
    });
    document.getElementById('filter-count').textContent = '검색 결과 '+visibleGroups+' / '+parentGroups.length+'개 사업 · '+visible.size+' / '+cards.length+'개 전형·회차';
    const groups = {cards: '#grants [data-notice-group]', calendar: '#calendar tbody tr[data-filter-id]', tasks: '#tasks tbody tr[data-filter-id]'};
    Object.entries(groups).forEach(([name, selector]) => {
      const rows = Array.from(document.querySelectorAll(selector));
      const empty = document.querySelector('[data-empty="' + name + '"]');
      const none = rows.length > 0 && !rows.some(row => !row.hidden);
      if (empty) empty.hidden = !none;
      if (name !== 'cards') {
        const table = document.querySelector('#' + (name === 'calendar' ? 'calendar' : 'tasks') + ' .table-wrap');
        if (table) table.hidden = none;
      }
    });
  }
  form.addEventListener('submit', event => event.preventDefault());
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  form.addEventListener('reset', () => { requestAnimationFrame(update); });
  document.getElementById('print-button').addEventListener('click', () => window.print());
  update();
})();
'''
