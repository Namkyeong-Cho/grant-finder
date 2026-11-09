"""Presentation of the extensive CV-matched research catalog."""

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .funding_catalog import AVAILABILITY, CATEGORIES, ROUTES, SUPPORT, priority_key, safe_url
from .countdown import countdown_html
from .program_groups import group_records


def e(value):
    return escape(str(value or ""), quote=True)


def link(label, url):
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(label)} ↗</a>' if safe_url(url) else e(label)


def deadline(item):
    value = item.get("deadline")
    if not value:
        return "일정 안내 확인"
    try:
        if "T" in value or ":" in value:
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not point.tzinfo:
                point = point.replace(tzinfo=ZoneInfo(item.get("deadline_timezone") or "Asia/Seoul"))
            return point.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d %H:%M 한국시간")
    except (ValueError, KeyError):
        return str(value) + " · 원문 확인"
    return str(value) + " · 원문 마감일자"


def remaining(item):
    value = item.get("deadline")
    fallback = "수시 신청 · 고정 마감 없음" if item.get("availability") == "rolling" else "마감일 미확정"
    precision = "datetime" if isinstance(value, str) and ("T" in value or ":" in value) else "date"
    return countdown_html(value, precision, item.get("deadline_timezone"), fallback)


def render_catalog(catalog, profile, definitions=None):
    candidates = catalog.get("candidates", [])
    if not candidates:
        return ""
    programs = group_records(candidates, "candidate", definitions if definitions is not None else catalog.get("program_groups"))
    program_by_item = {item['id']: group for group in programs for item in group['items']}
    by_id = {item['id']: item for item in candidates}
    current, chosen = [], set()
    preferred = [by_id[key] for key in profile.get('priority_program_ids', []) if key in by_id]
    for item in preferred + sorted(candidates, key=priority_key):
        group = program_by_item[item['id']]
        if group['id'] in chosen or item['fit'] != 'high' or item['availability'] not in {'open', 'rolling', 'announced'}:
            continue
        current.append((group, item))
        chosen.add(group['id'])
        if len(current) >= (8 if profile.get('priority_program_ids') else 6):
            break
    highlights = []
    for group, item in current:
        highlights.append(f'<a class="catalog-highlight" href="#program-{e(group["id"])}"><span>{e(AVAILABILITY[item["availability"]])} · {len(group["items"])}개 전형</span><strong>{e(group["title"])}</strong><small>{e(group["track_labels"].get(item["id"],item["title"]))} · {e(deadline(item))}</small>{remaining(item)}</a>')
    rows = []
    for group in programs:
        tracks, comparison = [], []
        for item in group['items']:
            excluded = item["fit"] == "excluded" or item["availability"] == "excluded"
            label = group['track_labels'].get(item['id'], item['title'])
            search = group['title'] + ' ' + label + ' ' + ' '.join(str(item.get(field, '')) for field in
                     ('title', 'sponsor', 'fit_reason', 'eligibility_summary', 'application_timing', 'next_action'))
            constraints = ''.join(f'<li>{e(text)}</li>' for text in item.get('personal_constraints', [])) or '<li>공고별 최종 조건과 제출 절차를 확인합니다.</li>'
            additional = ' · '.join(link('추가 공식 근거', url) for url in item.get('additional_sources', []) if safe_url(url))
            comparison.append(f'<tr data-catalog-comparison="{e(item["id"])}"><td><a href="#candidate-{e(item["id"])}" data-track-link>{e(label)}</a></td><td>{e(AVAILABILITY[item["availability"]])}</td><td>{e(deadline(item))}{remaining(item)}</td><td>{e(item["eligibility_summary"])}</td></tr>')
            tracks.append(f'''<details class="catalog-entry" id="candidate-{e(item['id'])}" data-catalog-entry data-catalog-search="{e(search)}" data-catalog-category="{e(item['category'])}" data-catalog-route="{e(item['route'])}" data-catalog-support="{e(item['support_type'])}" data-catalog-availability="{e(item['availability'])}" data-catalog-excluded="{'yes' if excluded else 'no'}">
<summary><span class="catalog-entry-heading"><strong>{e(label)}</strong><span>{e(SUPPORT.get(item['support_type'],item['support_type']))} · {e(ROUTES[item['route']])}</span></span><span class="badge {'closed' if excluded else ('open' if item['availability'] in ('open','rolling') else 'upcoming')}">{e(AVAILABILITY[item['availability']])}</span></summary>
<div class="catalog-entry-body"><p class="catalog-fit"><strong>CV와의 연결</strong>{e(item['fit_reason'])}</p><dl><div><dt>지원기관</dt><dd>{e(item['sponsor'])}</dd></div><div><dt>모집 일정</dt><dd>{e(item['application_timing'])}{remaining(item)}</dd></div><div><dt>신청자격</dt><dd>{e(item['eligibility_summary'])}</dd></div><div><dt>개인별 제약</dt><dd><ul>{constraints}</ul></dd></div><div><dt>다음 할 일</dt><dd>{e(item['next_action'])}</dd></div></dl><p class="catalog-evidence">{link('공식 원문',item['source_url'])} {additional}<span>확인일 {e(item['checked_on'])}</span></p><p class="muted small">{e(item.get('evidence_note',''))}</p></div></details>''')
        kinds = ' · '.join(dict.fromkeys(CATEGORIES.get(x['category'],x['category']) for x in group['items']))
        single_remaining = remaining(group['items'][0]) if len(group['items']) == 1 else '<span class="muted small">전형별 마감 비교</span>'
        rows.append(f'''<details class="catalog-program" id="program-{e(group['id'])}" data-catalog-group>
<summary><span class="catalog-entry-heading"><strong>{e(group['title'])}</strong><span>{e(kinds)}</span></span><span class="catalog-entry-meta"><span class="badge neutral" data-group-count>{len(group['items'])}개 전형</span>{single_remaining}</span></summary>
<div class="catalog-program-body"><div class="table-wrap catalog-comparison" tabindex="0" aria-label="전형별 자격과 마감 비교"><table><thead><tr><th>전형·회차</th><th>모집 상태</th><th>마감·남은 시간</th><th>주요 신청자격</th></tr></thead><tbody>{''.join(comparison)}</tbody></table></div>{''.join(tracks)}</div></details>''')
    def options(mapping):
        return "".join(f'<option value="{e(key)}">{e(value)}</option>' for key, value in mapping.items())
    project = (profile.get("active_projects") or [{}])[0]
    project_text = (f"{project.get('program','수행 과제')} {project.get('start_date','시작일 확인 필요')} ~ {project.get('end_date','종료일 확인 필요')} 수행 중."
                    if project else '현재 수행 과제 정보 확인이 필요합니다.')
    infra = profile.get('infrastructure_grant_awarded')
    infra_text = '신진인프라 미수혜' if infra is False else ('신진인프라 기수혜' if infra is True else '신진인프라 수혜 이력 확인 필요')
    confirmations = ' · '.join(x.get('statement','') for x in profile.get('user_confirmations',[]))
    return f'''<section id="funding-catalog" aria-labelledby="catalog-title">
<div class="section-heading"><div><p class="eyebrow">CV-BASED FUNDING SEARCH</p><h2 id="catalog-title">CV 기반 전체 지원 후보</h2></div><span class="section-tag">{len(programs)}개 사업 · {len(candidates)}개 세부 전형</span></div>
<p class="section-description">{e(profile.get('affiliation'))} · 수학 박사 {e(profile.get('phd_date'))} · 조교수 임용 {e(profile.get('first_faculty_date'))}. 연구비, 계산자원, 국제협력, 방문연구를 구분해 조사했습니다.</p>
<div class="catalog-profile"><strong>현재 과제와 함께 볼 조건</strong><p>{e(project_text)} {e(infra_text)}.</p><p>{e('사용자 확인: '+confirmations) if confirmations else '추가 수행·신청 과제는 프로필에 확인된 범위로 판단합니다.'}</p><p>일반 개인기초 신규와 병행 예외·기존 과제 교체 경로를 구분합니다. 해외 호스트·체류 요건은 후보별로 표시합니다.</p></div>
<h3 class="catalog-priority-title">먼저 검토할 경로</h3>
<div class="catalog-highlights">{''.join(highlights)}</div>
<p class="catalog-method">공식기관 자료를 대조한 조사 · 자료 확인 {e(catalog.get('checked_from',catalog.get('checked_on')))} ~ {e(catalog.get('checked_on'))}. 각 항목에 확인일을 표시합니다. 모집 여부와 최종 신청자격은 별도입니다. 상세 자동수집 중인 공고는 아래에 따로 있습니다.</p>
<form id="catalog-filters" class="catalog-filters" role="search"><label><span>전체 후보 검색</span><input id="catalog-search" type="search" placeholder="PDE, 반도체, 기후, 기관·사업명" autocomplete="off"></label><label><span>분야·지원기관군</span><select id="catalog-category"><option value="">전체 분야</option>{options(CATEGORIES)}</select></label><label><span>지원 형태</span><select id="catalog-support"><option value="">모든 지원 형태</option>{options(SUPPORT)}</select></label><label><span>신청 경로</span><select id="catalog-route"><option value="">모든 경로</option>{options(ROUTES)}</select></label><label><span>모집 상태</span><select id="catalog-availability"><option value="">모든 모집 상태</option>{options(AVAILABILITY)}</select></label><label><span>표시 범위</span><select id="catalog-scope"><option value="candidates">검토 후보</option><option value="all">제외 포함 전체</option><option value="excluded">현재 제외 사유</option></select></label><button class="button" type="reset">초기화</button></form>
<p id="catalog-count" class="muted small" aria-live="polite"></p><div class="catalog-entries">{''.join(rows)}</div><p id="catalog-empty" class="empty" hidden>조건에 맞는 후보가 없습니다. 검색어나 표시 범위를 바꿔 보세요.</p>
<details class="panel catalog-profile-details"><summary>CV 판정 근거와 남은 확인사항</summary><div class="panel-body"><p>로컬 CV의 Education·Professional Experience·Research Interests·Research Grants를 읽고 공개 소속·논문 자료와 대조했습니다.</p><p>연구 연결: {e(profile.get('research_summary'))}</p><p>확인 전 항목: {e(' · '.join(profile.get('unknown_information',[])))}</p><p>학위·임용일은 월 단위로 기재된 정밀도를 유지했습니다. 현재 과제와 신진인프라 이력은 사용자 답변을 반영했습니다.</p></div></details>
</section>'''


CATALOG_CSS = r'''
.catalog-profile{padding:18px 20px;border:1px solid #c6d9cf;border-left:4px solid var(--teal);border-radius:10px;background:#eef5ee;font-size:12px}.catalog-profile p{margin:6px 0 0;color:#4e655a}.catalog-highlights{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px;margin-top:18px}.catalog-highlight{padding:16px;border:1px solid var(--line);background:white;border-radius:10px;display:grid;align-content:start;gap:9px;color:var(--ink)}.catalog-highlight:hover{border-color:var(--teal);text-decoration:none}.catalog-highlight>span,.catalog-highlight>small{font-size:10px;color:var(--muted)}.catalog-highlight>strong{font-size:13px;line-height:1.6}.catalog-method{font-size:11px;line-height:1.8;color:var(--muted);margin:18px 0}.catalog-filters{display:grid;grid-template-columns:2fr repeat(5,minmax(0,1fr)) auto;gap:9px;align-items:end;padding:16px;background:#edf1eb;border-radius:10px}.catalog-filters label{display:grid;gap:6px;min-width:0}.catalog-filters label span{font-size:10px;color:var(--muted)}.catalog-filters input,.catalog-filters select{font-size:11px;height:40px;padding:8px}.catalog-filters button{height:40px}.catalog-entries{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}.catalog-entry+ .catalog-entry{border-top:1px solid var(--line)}.catalog-entry>summary{cursor:pointer;display:flex;justify-content:space-between;gap:20px;padding:16px 18px;list-style:none;align-items:center}.catalog-entry>summary::-webkit-details-marker{display:none}.catalog-entry>summary::after{content:'+';color:var(--teal);font-size:18px}.catalog-entry[open]>summary::after{content:'−'}.catalog-entry-heading{display:grid;gap:5px;flex:1;min-width:0}.catalog-entry-heading strong{font-size:13px;line-height:1.6;overflow-wrap:anywhere}.catalog-entry-heading>span,.catalog-entry-meta>.catalog-entry-route{font-size:10px;color:var(--muted)}.catalog-entry-meta{display:grid;justify-items:end;gap:6px;max-width:36%}.catalog-entry-body{padding:0 20px 19px;font-size:12px}.catalog-fit{padding:12px 14px;background:#f3f7f1;border-radius:8px;line-height:1.8}.catalog-fit strong{display:block;font-size:11px;color:var(--teal)}.catalog-entry-body dl>div{display:grid;grid-template-columns:85px 1fr;gap:14px;padding:10px 0;border-bottom:1px solid #edf0eb}.catalog-entry-body dt{font-size:11px;color:var(--muted)}.catalog-entry-body dd{margin:0;line-height:1.8;overflow-wrap:anywhere}.catalog-entry-body ul{margin:0;padding-left:17px}.catalog-evidence{display:flex;gap:12px;flex-wrap:wrap;font-size:11px}.catalog-evidence span{margin-left:auto;color:var(--muted)}.catalog-profile-details{margin-top:18px;font-size:12px}.catalog-profile-details>summary{font-size:12px}
@media(max-width:1150px){.catalog-filters{grid-template-columns:repeat(3,minmax(0,1fr))}.catalog-filters label:first-child{grid-column:span 2}.catalog-highlights{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.catalog-filters{grid-template-columns:repeat(2,minmax(0,1fr));padding:12px}.catalog-filters label:first-child{grid-column:1/-1}.catalog-filters button{align-self:end}.catalog-highlights{grid-template-columns:1fr}.catalog-entry>summary{padding:15px 12px;gap:9px;flex-wrap:wrap}.catalog-entry-heading{flex-basis:65%}.catalog-entry-meta{max-width:100%;justify-items:start;display:flex;align-items:center;flex-wrap:wrap}.catalog-entry-body{padding:0 13px 15px}.catalog-entry-body dl>div{grid-template-columns:1fr;gap:4px}.catalog-evidence span{margin-left:0}.catalog-profile{padding:14px;font-size:11px}}
@media print{.catalog-filters,#catalog-count,.catalog-method,.catalog-highlights{display:none!important}.catalog-entry-body{display:block!important}.catalog-entry{break-inside:avoid}.catalog-entries{overflow:visible}.catalog-profile{background:#fff}.catalog-entry>summary{padding:10px 0}.catalog-entry[hidden]{display:block!important}}
'''

CATALOG_CSS += r'''

.catalog-program+.catalog-program{border-top:1px solid var(--line)}
.catalog-program>summary{cursor:pointer;display:flex;justify-content:space-between;gap:20px;padding:18px;list-style:none;align-items:center}
.catalog-program>summary::-webkit-details-marker{display:none}
.catalog-program>summary::after{content:'+';color:var(--teal);font-size:20px}
.catalog-program[open]>summary::after{content:'−'}
.catalog-program[open]>summary{background:#edf4ef;border-bottom:1px solid var(--line)}
.catalog-program-body{padding:16px 18px 20px}
.catalog-comparison{margin-bottom:16px;max-height:440px}
.catalog-comparison td:first-child{min-width:150px;font-weight:650}
.catalog-comparison td:nth-child(3){min-width:190px}
.catalog-comparison td:last-child{min-width:240px}
.catalog-program-body .catalog-entry{border:1px solid var(--line);border-radius:8px;margin-top:10px}
@media(max-width:620px){.catalog-program>summary{padding:15px 12px;gap:9px}.catalog-program-body{padding:12px 10px}.catalog-program>summary .catalog-entry-meta{display:grid;justify-items:end;max-width:42%}}
@media print{.catalog-program-body{display:block!important}.catalog-program[hidden]{display:none!important}}
'''

CATALOG_JS = r'''
(() => {
 const form=document.getElementById('catalog-filters'); if(!form)return;
 const query=document.getElementById('catalog-search'),category=document.getElementById('catalog-category'),support=document.getElementById('catalog-support'),route=document.getElementById('catalog-route'),state=document.getElementById('catalog-availability'),scope=document.getElementById('catalog-scope');
 const entries=Array.from(document.querySelectorAll('[data-catalog-entry]'));
 const groups=Array.from(document.querySelectorAll('[data-catalog-group]'));
 function update(){
  let count=0,programCount=0;const text=query.value.trim().toLocaleLowerCase();
  const narrowed=Boolean(text||category.value||support.value||route.value||state.value||scope.value==='excluded');
  entries.forEach(entry=>{const d=entry.dataset;const excluded=d.catalogExcluded==='yes';const show=(!text||d.catalogSearch.toLocaleLowerCase().includes(text))&&(!category.value||d.catalogCategory===category.value)&&(!support.value||d.catalogSupport===support.value)&&(!route.value||d.catalogRoute===route.value)&&(!state.value||d.catalogAvailability===state.value)&&(scope.value==='all'||(scope.value==='excluded'?excluded:!excluded));entry.hidden=!show;if(show)count++;});
  groups.forEach(group=>{const children=Array.from(group.querySelectorAll('[data-catalog-entry]'));const visible=children.filter(entry=>!entry.hidden);group.hidden=visible.length===0;if(!group.hidden)programCount++;group.querySelector('[data-group-count]').textContent=visible.length+' / '+children.length+'개 전형';group.querySelectorAll('[data-catalog-comparison]').forEach(row=>{row.hidden=document.getElementById('candidate-'+row.dataset.catalogComparison).hidden;});if(narrowed&&!group.hidden)group.open=true;});
  document.getElementById('catalog-count').textContent='표시 '+programCount+'개 사업 · '+count+'개 전형 / 전체 '+groups.length+'개 사업 · '+entries.length+'개 전형';document.getElementById('catalog-empty').hidden=count>0;
 }
 function clear(){query.value='';category.value='';support.value='';route.value='';state.value='';}
 form.addEventListener('submit',event=>event.preventDefault());form.addEventListener('input',update);form.addEventListener('change',update);form.addEventListener('reset',()=>requestAnimationFrame(()=>{groups.forEach(group=>group.open=false);update();}));
 document.querySelectorAll('.catalog-highlight').forEach(anchor=>anchor.addEventListener('click',()=>{clear();scope.value='all';update();const target=document.getElementById(anchor.hash.slice(1));if(target)target.open=true;}));
 document.querySelectorAll('[data-track-link]').forEach(anchor=>anchor.addEventListener('click',()=>{const target=document.getElementById(anchor.hash.slice(1));if(target){target.open=true;target.closest('[data-catalog-group]').open=true;}}));
 update();
})();
'''
