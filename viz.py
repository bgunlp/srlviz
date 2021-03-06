import copy
import json
import os
from itertools import groupby
from operator import itemgetter

import conllu
from flask import Flask, render_template, request, redirect, url_for
from flask_bootstrap import Bootstrap
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import RadioField, SubmitField

app = Flask(__name__)
app.config['SECRET_KEY'] = 'msc'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///data.sqlite3'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
Bootstrap(app)
db = SQLAlchemy(app)

DATA_ROOT = os.path.join(app.root_path, 'static', 'dataset')


class AnnotationForm(FlaskForm):
    annotation = RadioField('Annotation', choices=[
        ('err_sent', 'Error in Sentence Alignment'),
        ('err_word', 'Error in Word Alignment'),
        ('poor_trans', 'Poor Translation'),
        ('poor_syn', 'Poor Syntactic Parsing'),
        ('poor_frame', 'Poor Frame Parsing'),
        ('ok', 'OK')
    ])
    submit = SubmitField('Annotate')


class Annotation(db.Model):
    __tablename__ = 'annotations'
    file = db.Column(db.String(40), primary_key=True)
    sentence = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(20))

    def __repr__(self):
        return f'<Annotation for {self.file}/{self.sentence}>'


def english_sents(filename):
    with open(os.path.join(DATA_ROOT, 'english_parsed', filename), encoding='utf-8') as f:
        english = conllu.parse(f.read())
    return [' '.join([row['form'] for row in sent]) for sent in english]


def get_head_of_span(tokens, start, end):
    return [token for token in tokens[start:end] if token['head'] not in range(start, end)]


def get_subtree_from_head(tokens, head):
    visited, stack = [], [head]
    while stack:
        token = stack.pop()
        if token not in visited:
            visited.append(token)
            stack.extend([t for t in tokens if t['head'] == token['id']])
    return sorted(visited, key=lambda t: t['id'])


def project_srl(english_srl, alignment, en_tokens, he_tokens):
    hebrew_srl = copy.deepcopy(english_srl)

    en2he_alignment = {}
    for key, group in groupby(sorted(alignment), key=itemgetter(0)):
        en2he_alignment[key] = list(map(itemgetter(1), group))
    try:
        for obj in hebrew_srl:
            span = obj['target']['spans'][0]
            head_of_span = get_head_of_span(en_tokens, span['start'], span['end'])
            if len(head_of_span) != 1:
                continue
            head_of_span, = head_of_span
            aligned_head = en2he_alignment[head_of_span['id'] - 1]
            if len(aligned_head) != 1:
                continue
            aligned_head, = aligned_head
            subtree = get_subtree_from_head(he_tokens, he_tokens[aligned_head])
            obj['target']['spans'][0]['start'] = subtree[0]['id'] - 1
            obj['target']['spans'][0]['end'] = subtree[-1]['id'] - 1
            for fe in obj['annotationSets'][0]['frameElements']:
                span = fe['spans'][0]
                head_of_span = get_head_of_span(en_tokens, span['start'], span['end'])
                if len(head_of_span) != 1:
                    continue
                head_of_span, = head_of_span
                aligned_head = en2he_alignment[head_of_span['id'] - 1]
                if len(aligned_head) != 1:
                    continue
                aligned_head, = aligned_head
                subtree = get_subtree_from_head(he_tokens, he_tokens[aligned_head])
                span['start'] = subtree[0]['id'] - 1
                span['end'] = subtree[-1]['id'] - 1
    except KeyError:
        return []
    return hebrew_srl


def create(filename):
    with open(os.path.join(DATA_ROOT, 'english_parsed', filename), encoding='utf-8') as f:
        english = conllu.parse(f.read())
    with open(os.path.join(DATA_ROOT, 'hebrew_parsed', filename), encoding='utf-8') as f:
        hebrew = conllu.parse(f.read())
    with open(os.path.join(DATA_ROOT, 'english_srl', filename), encoding='utf-8') as f:
        english_srl = [json.loads(line.strip()) for line in f]
    with open(os.path.join(DATA_ROOT, 'fastalign_outputs', filename), encoding='utf-8') as f:
        alignment = []
        for line in f:
            dashed_pairs = line.strip().split(' ')
            pairs = [(int(i), int(j)) for i, j in [p.split('-') for p in dashed_pairs]]
            alignment.append(pairs)
    sents = []
    for en, he, srl, alignment_ in zip(english, hebrew, english_srl, alignment):
        obj = {
            'imdbid': filename.split('_')[2],
            'english': {
                'frames': srl['frames'],
                'words': en
            },
            'hebrew': {
                'frames': project_srl(srl['frames'], alignment_, en, he),
                'words': he
            },
            'alignment': alignment_,
        }
        sents.append(obj)
    return sents


@app.route('/')
def index():
    total_annotations = len(Annotation.query.all())
    page = request.args.get('page', 1, type=int)
    files = Annotation.query \
        .with_entities(Annotation.file, func.count(Annotation.file)) \
        .group_by(Annotation.file) \
        .order_by(func.count(Annotation.file).desc()) \
        .paginate(page, 10, False)
    annotations_by_file = dict(files.items)
    next_url = url_for('index', page=files.next_num) if files.has_next else None
    prev_url = url_for('index', page=files.prev_num) if files.has_prev else None
    return render_template('index.html',
                           total_annotations=total_annotations,
                           annotations_by_file=annotations_by_file,
                           next_url=next_url,
                           prev_url=prev_url,
                           title='File Selection')


@app.route('/<filename>')
def sentence_select(filename):
    sents = english_sents(filename)
    page = request.args.get('page', 1, type=int)
    annotations = Annotation.query.filter_by(file=filename)
    total_annotations = len(annotations.all())
    annotations_page = annotations.paginate(page, 10, False)
    sent2annotation = {a.sentence: a for a in annotations_page.items}
    annotated_sents = []
    for sent_id, sent in enumerate(sents):
        annotation = sent2annotation.get(sent_id)
        if annotation:
            annotated_sents.append((sent, annotation.message))
        else:
            annotated_sents.append((sent, 'none'))
    next_url = url_for('sentence_select', filename=filename, page=annotations_page.next_num) \
        if annotations_page.has_next else None
    prev_url = url_for('sentence_select', filename=filename, page=annotations_page.prev_num) \
        if annotations_page.has_prev else None
    return render_template('sentenceselect2.html',
                           filename=filename,
                           sents=annotated_sents,
                           total_annotations=total_annotations,
                           next_url=next_url,
                           prev_url=prev_url,
                           title='Sentence Selection')


@app.route('/<filename>/<sent_id>', methods=['GET', 'POST'])
def tree_view(filename, sent_id):
    annotation = Annotation.query.filter_by(file=filename, sentence=sent_id).first()
    if request.method == 'POST':
        form = AnnotationForm()
        if annotation:
            annotation.message = form.annotation.data
        else:
            db.session.add(Annotation(file=filename, sentence=sent_id, message=form.annotation.data))
        db.session.commit()
        return redirect(url_for('sentence_select', filename=filename))
    form = AnnotationForm(annotation=annotation.message) if annotation else AnnotationForm()
    data = create(filename)
    return render_template('treeview.html', data=data[int(sent_id)], title='Graphic', form=form)


if __name__ == '__main__':
    app.run()
