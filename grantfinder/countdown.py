"""Live countdowns that preserve the precision and timezone of each deadline."""

import re
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo


def countdown_html(value, precision="datetime", timezone=None, fallback="마감일 미확정"):
    empty = '<span class="deadline-countdown muted">' + escape(str(fallback)) + '</span>'
    if not isinstance(value, str):
        return empty
    try:
        if precision == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return empty
            date.fromisoformat(value)
            zone = timezone or "Asia/Seoul"
            ZoneInfo(zone)
            return ('<span class="deadline-countdown" data-countdown-date="' + escape(value, quote=True)
                    + '" data-countdown-zone="' + escape(zone, quote=True) + '">남은 날짜 계산 중</span>')
        if precision != "datetime" or not re.match(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", value):
            return empty
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if point.tzinfo is None:
            if not timezone:
                return empty
            point = point.replace(tzinfo=ZoneInfo(timezone))
        return ('<span class="deadline-countdown" data-countdown-at="'
                + escape(point.isoformat(), quote=True) + '">남은 시간 계산 중</span>')
    except (ValueError, TypeError, KeyError):
        return empty


COUNTDOWN_CSS = r'''
.deadline-countdown,.catalog-highlight>.deadline-countdown{display:block;margin-top:5px;font-size:11px;font-weight:650;line-height:1.6;color:var(--teal);overflow-wrap:anywhere}
.deadline-countdown[data-urgency="soon"]{color:var(--amber)}
.deadline-countdown[data-urgency="urgent"]{color:#b34536}
.deadline-countdown[data-urgency="expired"],.deadline-countdown.muted{color:var(--muted);font-weight:400}
.countdown-context{font-size:10px;color:var(--muted);margin:10px 0 0;line-height:1.8}
.catalog-entry-meta .deadline-countdown{font-size:10px;text-align:right}
@media(max-width:620px){.catalog-entry-meta .deadline-countdown{text-align:left}}
'''


COUNTDOWN_JS = r'''
(() => {
  'use strict';
  const timers = Array.from(document.querySelectorAll('[data-countdown-at], [data-countdown-date]'));
  function calendarDay(now, zone) {
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone:zone, year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(new Date(now));
    const field = name => Number(parts.find(part => part.type === name).value);
    return Date.UTC(field('year'), field('month') - 1, field('day')) / 86400000;
  }
  function tick() {
    const now = Date.now();
    timers.forEach(element => {
      try {
        if (element.dataset.countdownAt) {
          const end = Date.parse(element.dataset.countdownAt);
          if (!Number.isFinite(end)) throw new Error('Invalid deadline');
          const remaining = end - now;
          if (remaining <= 0) {
            element.textContent = '마감 시각 지남';
            element.dataset.urgency = 'expired';
            return;
          }
          const minutes = Math.ceil(remaining / 60000);
          const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), rest = minutes % 60;
          element.textContent = '마감까지 ' + (days ? days + '일 ' : '') + (days || hours ? hours + '시간 ' : '') + rest + '분';
          element.dataset.urgency = remaining <= 86400000 ? 'urgent' : remaining <= 604800000 ? 'soon' : 'normal';
        } else {
          const target = element.dataset.countdownDate.split('-').map(Number);
          const days = Date.UTC(target[0], target[1] - 1, target[2]) / 86400000 - calendarDay(now, element.dataset.countdownZone);
          element.textContent = days > 0 ? '마감일까지 D-' + days + ' · 시각 미기재' : days === 0 ? '오늘 마감일 · 시각 미기재' : '마감일 지남 · 시각 미기재';
          element.dataset.urgency = days < 0 ? 'expired' : days === 0 ? 'urgent' : days <= 7 ? 'soon' : 'normal';
        }
      } catch (_) {
        element.textContent = '마감 시각·시간대 확인 필요';
        element.dataset.urgency = 'unknown';
      }
    });
    const clock = document.getElementById('countdown-clock');
    if (clock) clock.textContent = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).format(new Date(now));
  }
  tick();
  setInterval(tick, 60000);
  document.addEventListener('visibilitychange', () => {if (!document.hidden) tick();});
  window.addEventListener('pageshow', tick);
})();
'''
