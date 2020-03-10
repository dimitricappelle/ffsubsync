#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import logging
import os
import sys

import numpy as np
from sklearn.pipeline import Pipeline

from .aligners import FFTAligner, MaxScoreAligner
from .speech_transformers import (
        SubtitleSpeechTransformer,
        VideoSpeechTransformer,
        DeserializeSpeechTransformer
)
from .subtitle_parsers import GenericSubtitleParser, SubtitleOffseter, SubtitleScaler
from .version import __version__

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 100

FRAMERATE_RATIOS = [24./23.976, 25./23.976, 25./24.]

DEFAULT_FRAME_RATE = 48000
DEFAULT_ENCODING = 'infer'
DEFAULT_MAX_SUBTITLE_SECONDS = 10
DEFAULT_START_SECONDS = 0
DEFAULT_SCALE_FACTOR = 1
DEFAULT_VAD = 'webrtc'
DEFAULT_MAX_OFFSET_SECONDS = 600


def override(args, **kwargs):
    args_dict = dict(args.__dict__)
    args_dict.update(kwargs)
    return args_dict


def make_srt_parser(
        fmt,
        encoding=DEFAULT_ENCODING,
        max_subtitle_seconds=DEFAULT_MAX_SUBTITLE_SECONDS,
        start_seconds=DEFAULT_START_SECONDS,
        **kwargs
):
    return GenericSubtitleParser(
        fmt=fmt,
        encoding=encoding,
        max_subtitle_seconds=max_subtitle_seconds,
        start_seconds=start_seconds
    )


def make_srt_speech_pipeline(
        fmt='srt',
        encoding=DEFAULT_ENCODING,
        max_subtitle_seconds=DEFAULT_MAX_SUBTITLE_SECONDS,
        start_seconds=DEFAULT_START_SECONDS,
        scale_factor=DEFAULT_SCALE_FACTOR,
        parser=None,
        **kwargs
):
    if parser is None:
        parser = make_srt_parser(
            fmt,
            encoding=encoding,
            max_subtitle_seconds=max_subtitle_seconds,
            start_seconds=start_seconds
        )
    assert parser.encoding_to_use == encoding
    assert parser.max_subtitle_seconds == max_subtitle_seconds
    assert parser.start_seconds == start_seconds
    return Pipeline([
        ('parse', parser),
        ('scale', SubtitleScaler(scale_factor)),
        ('speech_extract', SubtitleSpeechTransformer(
            sample_rate=SAMPLE_RATE,
            start_seconds=start_seconds,
            framerate_ratio=scale_factor,
        ))
    ])


def main():
    parser = argparse.ArgumentParser(description='Synchronize subtitles with video.')
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s {version}'.format(version=__version__))
    parser.add_argument('reference',
                        help='Reference (video, srt, or a numpy array with VAD speech) '
                             'to which to synchronize input subtitles.')
    parser.add_argument('-i', '--srtin', help='Input subtitles file (default=stdin).')
    parser.add_argument('-o', '--srtout', help='Output subtitles file (default=stdout).')
    parser.add_argument('--encoding', default=DEFAULT_ENCODING,
                        help='What encoding to use for reading input subtitles '
                        '(default=%s).' % DEFAULT_ENCODING)
    parser.add_argument('--max-subtitle-seconds', type=float, default=DEFAULT_MAX_SUBTITLE_SECONDS,
                        help='Maximum duration for a subtitle to appear on-screen '
                             '(default=%.3f seconds).' % DEFAULT_MAX_SUBTITLE_SECONDS)
    parser.add_argument('--start-seconds', type=int, default=DEFAULT_START_SECONDS,
                        help='Start time for processing '
                             '(default=%d seconds).' % DEFAULT_START_SECONDS)
    parser.add_argument('--max-offset-seconds', type=int, default=DEFAULT_MAX_OFFSET_SECONDS,
                        help='The max allowed offset seconds for any subtitle segment '
                             '(default=%d seconds).' % DEFAULT_MAX_OFFSET_SECONDS)
    parser.add_argument('--frame-rate', type=int, default=DEFAULT_FRAME_RATE,
                        help='Frame rate for audio extraction (default=%d).' % DEFAULT_FRAME_RATE)
    parser.add_argument('--output-encoding', default='same',
                        help='What encoding to use for writing output subtitles '
                             '(default=same as for input).')
    parser.add_argument('--reference-encoding',
                        help='What encoding to use for reading / writing reference subtitles '
                             '(if applicable, default=infer).')
    parser.add_argument('--vad', choices=['webrtc', 'auditok'], default=None,
                        help='Which voice activity detector to use for speech extraction '
                             '(if using video / audio as a reference, default=webrtc).')
    parser.add_argument('--no-fix-framerate', action='store_true',
                        help='If specified, subsync will not attempt to correct a framerate '
                             'mismatch between reference and subtitles.')
    parser.add_argument('--serialize-speech', action='store_true',
                        help='If specified, serialize reference speech to a numpy array.')
    parser.add_argument('--vlc-mode', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.vlc_mode:
        logger.setLevel(logging.CRITICAL)
    ref_format = args.reference[-3:]
    if ref_format in ('srt', 'ssa', 'ass'):
        if args.vad is not None:
            logger.warning('Vad specified, but reference was not a movie')
        reference_pipe = make_srt_speech_pipeline(
            **override(
                args,
                parser=make_srt_parser(
                    ref_format,
                    **override(args, encoding=args.reference_encoding or 'infer')
                )
            )
        )
    elif ref_format == 'npy':
        if args.vad is not None:
            logger.warning('Vad specified, but reference was not a movie')
        reference_pipe = Pipeline([
            ('deserialize', DeserializeSpeechTransformer())
        ])
    else:
        vad = args.vad or DEFAULT_VAD
        if args.reference_encoding is not None:
            logger.warning('Reference srt encoding specified, but reference was a video file')
        reference_pipe = Pipeline([
            ('speech_extract', VideoSpeechTransformer(vad=vad,
                                                      sample_rate=SAMPLE_RATE,
                                                      frame_rate=args.frame_rate,
                                                      start_seconds=args.start_seconds,
                                                      vlc_mode=args.vlc_mode))
        ])
    if args.no_fix_framerate:
        framerate_ratios = [1.]
    else:
        framerate_ratios = np.concatenate([
            [1.], np.array(FRAMERATE_RATIOS), 1./np.array(FRAMERATE_RATIOS)
        ])
    if args.serialize_speech:
        logger.info('serializing speech...')
        npy_savename = os.path.splitext(args.reference)[0] + '.npy'
        np.save(npy_savename, reference_pipe.transform(None))
        logger.info('...done')
        if args.srtin is None:
            logger.info('unsynchronized subtitle file not specified; skipping synchronization')
            return 0
    parser = make_srt_parser(fmt=args.srtin[-3:], **args.__dict__)
    logger.info("extracting speech segments from subtitles '%s'...", args.srtin)
    srt_pipes = [
        make_srt_speech_pipeline(
            **override(args, scale_factor=scale_factor, parser=parser)
        ).fit(args.srtin)
        for scale_factor in framerate_ratios
    ]
    logger.info('...done')
    logger.info("extracting speech segments from reference '%s'...", args.reference)
    reference_pipe.fit(args.reference)
    logger.info('...done')
    logger.info('computing alignments...')
    offset_samples, best_srt_pipe = MaxScoreAligner(
        FFTAligner, SAMPLE_RATE, args.max_offset_seconds
    ).fit_transform(
        reference_pipe.transform(args.reference),
        srt_pipes,
    )
    logger.info('...done')
    offset_seconds = offset_samples / float(SAMPLE_RATE)
    scale_step = best_srt_pipe.named_steps['scale']
    logger.info('offset seconds: %.3f', offset_seconds)
    logger.info('framerate scale factor: %.3f', scale_step.scale_factor)
    offseter = SubtitleOffseter(offset_seconds).fit_transform(scale_step.subs_)
    if args.output_encoding != 'same':
        offseter = offseter.set_encoding(args.output_encoding)
    offseter.write_file(args.srtout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
