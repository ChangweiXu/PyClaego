#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from bs4 import BeautifulSoup

NOISE_SELECTORS = [
    'header.arxiv-html-header',
    'nav.ltx_page_navbar',
    'nav.ltx_TOC',
    'ol.ltx_toclist',
    '.ltx_tocentry',
    'div#infobox.infobox',
    'div.ltx_authors',
    'dialog#modal-form',
    '.ltx_pagination.ltx_role_newpage',
]


def clean_text(text: str) -> str:
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def section_heading(section):
    for selector in [
        'h2.ltx_title_section',
        'h3.ltx_title_subsection',
        'h4.ltx_title_subsubsection',
        'h5',
        'h6',
    ]:
        tag = section.select_one(selector)
        if tag:
            return clean_text(tag.get_text(' ', strip=True))
    return 'Unnamed Section'


def extract_paragraphs(node):
    out = []
    for p in node.select('div.ltx_para p.ltx_p, p.ltx_p'):
        txt = clean_text(p.get_text(' ', strip=True))
        if txt and txt not in out:
            out.append(txt)
    return out


def extract_captions(node):
    caps = []
    for c in node.select('figcaption.ltx_caption, caption.ltx_caption'):
        txt = clean_text(c.get_text(' ', strip=True))
        if txt:
            caps.append(txt)
    return caps


def prune_noise(soup):
    for selector in NOISE_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()


def parse_html(html: str):
    soup = BeautifulSoup(html, 'html.parser')
    prune_noise(soup)

    title = 'Unknown Title'
    title_tag = soup.select_one('h1.ltx_title_document')
    if title_tag:
        title = clean_text(title_tag.get_text(' ', strip=True))

    abstract_paras = []
    for p in soup.select('div#abstract1.ltx_abstract p.ltx_p'):
        txt = clean_text(p.get_text(' ', strip=True))
        if txt:
            abstract_paras.append(txt)

    sections = []
    for sec in soup.select('article.ltx_document section.ltx_section'):
        heading = section_heading(sec)
        if 'authors' in heading.lower() or heading.lower().startswith('7 authors'):
            continue
        paras = extract_paragraphs(sec)
        captions = extract_captions(sec)
        if paras or captions:
            sections.append({
                'heading': heading,
                'paragraphs': paras,
                'captions': captions,
            })

    return {
        'title': title,
        'abstract': abstract_paras,
        'sections': sections,
    }


def to_markdown(data: dict) -> str:
    lines = [f"# {data['title']}", '']
    if data.get('abstract'):
        lines += ['## Abstract', '']
        lines += data['abstract']
        lines += ['']

    for sec in data.get('sections', []):
        lines += [f"## {sec['heading']}", '']
        lines += sec.get('paragraphs', [])
        if sec.get('captions'):
            lines += ['', '### Captions', '']
            for cap in sec['captions']:
                lines.append(f'- {cap}')
        lines += ['']
    return '\n'.join(lines).strip() + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input_html')
    ap.add_argument('output_md')
    ap.add_argument('--output-info', default=None)
    args = ap.parse_args()

    input_path = Path(args.input_html)
    output_md = Path(args.output_md)
    html = input_path.read_text(encoding='utf-8')
    data = parse_html(html)
    md = to_markdown(data)
    output_md.write_text(md, encoding='utf-8')

    if args.output_info:
        info = {
            'title': data['title'],
            'abstract_paragraphs': len(data.get('abstract', [])),
            'sections': [s['heading'] for s in data.get('sections', [])],
            'section_count': len(data.get('sections', [])),
            'input_html': str(input_path),
            'output_md': str(output_md),
        }
        Path(args.output_info).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps({
        'title': data['title'],
        'section_count': len(data.get('sections', [])),
        'output_md': str(output_md)
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
