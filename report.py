#!/usr/bin/env python3
"""linemon report - an evidence report for a period, as one HTML page to send to an ISP.

  report.py /var/lib/linemon --from 2026-10-01T18:00 --to 2026-10-08T18:00 --lang es -o report.html

Open the file in a browser and print it to PDF if a PDF is wanted. It states the period, how
much of it the monitor observed, availability, every internet outage with where the path broke
and what the router reported, how it was measured, and the linemon version. Every figure comes
from analyze.py, the same as the page and the analyzer. It holds no personal details, addresses
or router credentials; add those in the message it goes with, if the ISP needs them.
"""
import argparse
import datetime as dt
import html
import sys

import analyze

TEXT = {
    'en': {
        'title': 'Internet line outage report',
        'period': 'Period', 'generated': 'Generated', 'version': 'Measured and reported by',
        'version_text': 'linemon {v}, an independent monitor connected by cable to the ISP router',
        'summary': 'Summary', 'counted': 'Period counted', 'before': 'Before monitoring began (not counted)',
        'observed': 'Observed', 'unknown': 'Unknown (monitor not running, its cable down, or unhealthy)',
        'outages': 'Internet outages (3 s or more)', 'downtime': 'Time without internet',
        'availability': 'Availability (of observed time)', 'longest': 'Longest outage',
        'mttr': 'Mean time to recovery (MTTR)', 'mtbf': 'Mean time between failures (MTBF)',
        'none': 'none', 'low_confidence': 'More than 1 % of the period is unknown: the figures are less certain.',
        'where': 'Where the path broke', 'count': 'Outages', 'time': 'Time',
        'states': 'What the ISP router reported during these outages', 'share': 'Share',
        'states_note': 'Time in each state the router reported, summed over the outages listed below. A state '
                       'counts from the capture that first saw it until the next state was seen, so the times are '
                       'precise to about 30 s.',
        'not_captured': 'not known (before the first capture of an outage, or captures failed)',
        'list': 'Outages', 'start': 'Start', 'end': 'End', 'duration': 'Duration', 'layer': 'Where it broke',
        'router': 'What the ISP router reported', 'during': 'During the outage', 'ongoing': 'ongoing',
        'no_outages': 'No internet outages in this period.',
        'method': 'How it was measured',
        'method_items': [
            'Once a second the monitor checks, over its own cable, its link, the ISP router, the first '
            'two routers in the operator\'s network, three internet hosts (1.1.1.1, 8.8.8.8, 9.9.9.9) and DNS.',
            'An internet outage is a period when all three internet hosts failed at once, from the first '
            'failed check; a host counts as down after 3 failed checks in a row, so outages under 3 s are not counted.',
            '"Where it broke" is the first layer that was already down when the outage began.',
            'Time when the monitor was not running, its own cable was down or it could not trust its '
            'measurements is unknown, never counted as up.',
            'Times are the monitor\'s local time, synchronised by NTP.',
        ],
    },
    'es': {
        'title': 'Informe de cortes de la línea de internet',
        'period': 'Periodo', 'generated': 'Generado', 'version': 'Medido e informado por',
        'version_text': 'linemon {v}, un monitor independiente conectado por cable al router del operador',
        'summary': 'Resumen', 'counted': 'Periodo contado', 'before': 'Antes de empezar la monitorización (no contado)',
        'observed': 'Observado', 'unknown': 'Desconocido (monitor parado, su cable desconectado o sin fiabilidad)',
        'outages': 'Cortes de internet (3 s o más)', 'downtime': 'Tiempo sin internet',
        'availability': 'Disponibilidad (del tiempo observado)', 'longest': 'Corte más largo',
        'mttr': 'Tiempo medio de recuperación (MTTR)', 'mtbf': 'Tiempo medio entre fallos (MTBF)',
        'none': 'ninguno', 'low_confidence': 'Más del 1 % del periodo es desconocido: las cifras son menos seguras.',
        'where': 'Dónde se cortó', 'count': 'Cortes', 'time': 'Tiempo',
        'states': 'Qué indicaba el router del operador durante estos cortes', 'share': 'Proporción',
        'states_note': 'Tiempo en cada estado que indicaba el router, sumado sobre los cortes de la lista. Un estado '
                       'cuenta desde la lectura que lo vio por primera vez hasta que se vio el siguiente, así que '
                       'los tiempos tienen una precisión de unos 30 s.',
        'not_captured': 'desconocido (antes de la primera lectura de un corte, o lecturas fallidas)',
        'list': 'Cortes', 'start': 'Inicio', 'end': 'Fin', 'duration': 'Duración', 'layer': 'Dónde se cortó',
        'router': 'Qué indicaba el router del operador', 'during': 'Durante el corte', 'ongoing': 'en curso',
        'no_outages': 'No hubo cortes de internet en este periodo.',
        'method': 'Cómo se midió',
        'method_items': [
            'Cada segundo el monitor comprueba, por su propio cable, su enlace, el router del operador, los dos '
            'primeros routers de la red del operador, tres servidores de internet (1.1.1.1, 8.8.8.8, 9.9.9.9) y el DNS.',
            'Un corte de internet es un periodo en el que fallan a la vez los tres servidores, desde la primera '
            'comprobación fallida; un servidor cuenta como caído tras 3 fallos seguidos, así que no se cuentan cortes de menos de 3 s.',
            '"Dónde se cortó" es la primera capa que ya estaba caída cuando empezó el corte.',
            'El tiempo en que el monitor no funcionaba, su cable estaba desconectado o no podía fiarse de sus '
            'medidas es desconocido, nunca se cuenta como disponible.',
            'Las horas son la hora local del monitor, sincronizada por NTP.',
        ],
        # what the analyzer and the router capture say, in the words the hand-made reports used
        'words': [
            ('monitor cable/port down', 'cable del monitor desconectado'),
            ('ISP router not responding', 'el router del operador no responde'),
            ('first ISP hop unreachable (access network)', 'primer router del operador inaccesible (red de acceso)'),
            ('second ISP hop unreachable', 'segundo router del operador inaccesible'),
            ('beyond the ISP hops (router and first hops answered)',
             'más allá de los primeros routers del operador (estos respondían)'),
            ('cable link down', 'cable del monitor desconectado'), ('still down', 'aún caído'),
            ('capture failed', 'lectura fallida'), ('NO SIGNAL (LOS)', 'SIN SEÑAL (LOS)'),
            ('signal OK', 'señal OK'), ('internet up', 'internet conectado'), ('no IP', 'sin IP'),
            ('fibre ', 'fibra '), ('router not reachable', 'router inaccesible'),
            ('reading status failed', 'lectura del estado fallida'),
        ],
    },
}


def translate(text, lang):
    for english, other in TEXT[lang].get('words', []):
        text = text.replace(english, other)
    return text


def pct(value, lang, places=2):
    text = f'{100 * value:.{places}f} %'
    return text.replace('.', ',') if lang == 'es' else text


def when(t):
    return f'{t:%d/%m/%Y %H:%M:%S}'


def outages_in(data_dir, lo, hi, now):
    """Internet outages that began in [lo, hi), oldest first, with what the analyzer says about each."""
    outages, periods = analyze.load_outages(data_dir)
    if not periods:
        return []
    data_end = min(now, periods[-1][1] or now)
    captures = analyze.load_captures(data_dir)
    rows = []
    for s, e, ongoing in analyze.intersect_all([outages.get(h, []) for h in analyze.INTERNET_HOSTS], open_end=data_end):
        if lo <= s < hi:
            rows.append({'start': s, 'end': None if ongoing else e, 'duration_s': (e - s).total_seconds(),
                         'layer': analyze.classify(s, e, outages),
                         'during': analyze.describe_during(analyze.during(s, e, outages)),
                         'router': analyze.router_during(captures, s, e),
                         'states': analyze.router_states(captures, s, e)[0]})
    return rows


def render(data_dir, lo, hi, lang='en', now=None):
    now = now or dt.datetime.now().astimezone()
    t = TEXT[lang]
    esc = lambda text: html.escape(translate(str(text), lang))
    a = analyze.availability(data_dir, lo, hi, now)
    rows = outages_in(data_dir, lo, hi, now)

    summary = []
    if a and a['before_s']:
        summary.append((t['before'], analyze.fmt_dur(a['before_s'])))
    if a and a['period_s']:
        summary += [(t['counted'], analyze.fmt_dur(a['period_s'])),
                    (t['observed'], f"{analyze.fmt_dur(a['observed_s'])} ({pct(a['observed_s'] / a['period_s'], lang)})"),
                    (t['unknown'], f"{analyze.fmt_dur(a['unknown_s'])} ({pct(a['unknown_s'] / a['period_s'], lang)})")]
        if a['observed_s']:
            summary += [(t['outages'], str(a['outages'])), (t['downtime'], analyze.fmt_dur(a['downtime_s'])),
                        (t['availability'], pct(a['availability'], lang, 4))]
            if rows:
                longest = max(rows, key=lambda r: r['duration_s'])
                summary.append((t['longest'], f"{analyze.fmt_dur(longest['duration_s'])} ({when(longest['start'])})"))
            summary += [(t['mttr'], analyze.fmt_dur(a['mttr_s']) if a['mttr_s'] is not None else t['none']),
                        (t['mtbf'], analyze.fmt_dur(a['mtbf_s']) if a['mtbf_s'] is not None else t['none'])]

    by_layer = {}  # counts only: the time without internet is in the summary, over observed time
    for r in rows:
        by_layer[r['layer']] = by_layer.get(r['layer'], 0) + 1

    out = [f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">',
           f'<title>{esc(t["title"])}</title><style>{STYLE}</style></head><body>',
           f'<h1>{esc(t["title"])}</h1>',
           '<table class="meta">',
           f'<tr><th>{esc(t["period"])}</th><td>{when(lo)} – {when(hi)}</td></tr>',
           f'<tr><th>{esc(t["generated"])}</th><td>{when(now)}</td></tr>',
           f'<tr><th>{esc(t["version"])}</th><td>{esc(t["version_text"].format(v=analyze.version()))}</td></tr>',
           '</table>', f'<h2>{esc(t["summary"])}</h2><table class="summary">']
    out += [f'<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>' for k, v in summary]
    out.append('</table>')
    if a and a.get('low_confidence'):
        out.append(f'<p class="note">{esc(t["low_confidence"])}</p>')
    if by_layer:
        out.append(f'<h2>{esc(t["where"])}</h2><table><tr><th></th><th>{esc(t["count"])}</th></tr>')
        out += [f'<tr><td>{esc(layer)}</td><td class="num">{n}</td></tr>'
                for layer, n in sorted(by_layer.items(), key=lambda x: -x[1])]
        out.append('</table>')
    in_state = {}  # summary -> seconds, over the outages listed
    for r in rows:
        for summary, seen, until in r['states']:
            in_state[summary] = in_state.get(summary, 0) + (until - seen).total_seconds()
    if in_state:
        total = sum(r['duration_s'] for r in rows)
        in_state[None] = max(0.0, total - sum(in_state.values()))
        out.append(f'<h2>{esc(t["states"])}</h2><p class="small">{esc(t["states_note"])}</p>'
                   f'<table><tr><th></th><th>{esc(t["time"])}</th><th>{esc(t["share"])}</th></tr>')
        out += [f'<tr><td>{esc(summary or t["not_captured"])}</td><td class="num nw">{esc(analyze.fmt_dur(secs))}</td>'
                f'<td class="num">{pct(secs / total, lang, 1)}</td></tr>'
                for summary, secs in sorted(in_state.items(), key=lambda x: (x[0] is None, -x[1])) if secs >= 0.5]
        out.append('</table>')
    out.append(f'<h2>{esc(t["list"])}</h2>')
    if rows:
        out.append(f'<table class="list"><tr><th>{esc(t["start"])}</th><th>{esc(t["end"])}</th><th>{esc(t["duration"])}</th>'
                   f'<th>{esc(t["layer"])}</th><th>{esc(t["router"])}</th></tr>')
        for r in rows:
            layer = esc(r['layer']) + (f'<br><small>{esc(t["during"])}: {esc(r["during"])}</small>' if r['during'] else '')
            out.append(f'<tr><td class="nw">{when(r["start"])}</td><td class="nw">{when(r["end"]) if r["end"] else esc(t["ongoing"])}</td>'
                       f'<td class="num nw">{esc(analyze.fmt_dur(r["duration_s"]))}</td><td>{layer}</td><td>{esc(r["router"]).replace(chr(10), "<br>")}</td></tr>')
        out.append('</table>')
    else:
        out.append(f'<p>{esc(t["no_outages"])}</p>')
    out.append(f'<h2>{esc(t["method"])}</h2><ul>' + ''.join(f'<li>{esc(item)}</li>' for item in t['method_items']) + '</ul>')
    out.append('</body></html>\n')
    return '\n'.join(out)


STYLE = """
body { font: 13px/1.45 -apple-system, system-ui, sans-serif; color: #111; max-width: 960px; margin: 24px auto; padding: 0 16px }
h1 { font-size: 20px } h2 { font-size: 15px; margin-top: 22px }
table { border-collapse: collapse; margin: 6px 0 } th, td { text-align: left; padding: 3px 10px 3px 0; vertical-align: top }
table.list th, table.list td { border-bottom: 1px solid #ddd } .num { text-align: right } .nw { white-space: nowrap }
small { color: #555 } .note { color: #8a5a00 }
@media print { body { margin: 0; max-width: none } tr { break-inside: avoid } }
"""


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('data', nargs='?', default='/var/lib/linemon', help='linemon data directory')
    p.add_argument('--from', dest='from_', type=analyze.local_time, metavar='TIME', required=True,
                   help='start of the period, e.g. 2026-10-01T18:00 (local time)')
    p.add_argument('--to', type=analyze.local_time, metavar='TIME', help='end of the period (default: now)')
    p.add_argument('--lang', choices=sorted(TEXT), default='en', help='language of the report')
    p.add_argument('-o', '--output', help='write the report here (default: standard output)')
    p.add_argument('--version', action='version', version=f'linemon {analyze.version()}')
    args = p.parse_args()
    now = dt.datetime.now().astimezone()
    page = render(args.data, args.from_, min(args.to or now, now), args.lang, now)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(page)
    else:
        sys.stdout.write(page)


if __name__ == '__main__':
    main()
