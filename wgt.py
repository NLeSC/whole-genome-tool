#!/usr/bin/env python
import sys
import argparse
import os
import uuid
import shutil
import tarfile
import os.path
import urlparse
import pickle
import json
import time

import numpy as np
from mapraline import *
from mapraline.component import PrositePatternAnnotator
from praline import load_score_matrix, load_sequence_fasta, open_builtin
from praline import write_alignment_clustal, write_alignment_fasta
from praline.core import *
from praline.container import TRACK_ID_INPUT, TRACK_ID_PREPROFILE
from praline.container import ALPHABET_DNA, PlainTrack, SequenceTree
from praline.component import PairwiseAligner, ProfileBuilder
from praline.component import GuideTreeBuilder, TreeMultipleSequenceAligner
from praline.component import DummyMasterSlaveAligner
from praline.util import run, write_log_structure
from praline.util import HierarchicalClusteringAlgorithm

from newick import get_tree, tree_distance
from manager import ConstellationManager


ROOT_TAG = "__ROOT__"
_TRACK_ID_BASE_PATTERN = "mapraline.track.MotifAnnotationPattern"
_TRACK_ID_BASE_FILE = "mapraline.track.MotifAnnotationFile"

_unicode_to_str = lambda l: [x.encode('ascii') for x in l]



def main():
    with open(sys.argv[1], 'rb') as jobfile:
        jobs = [_unicode_to_str(job) for job in json.load(jobfile)]

    # Setup the execution manager.
    index = TypeIndex()
    index.autoregister()
    manager = ConstellationManager(index)

    parser = args_parser()

    root_node = TaskNode(ROOT_TAG)
    start = time.time()
    # Create all MSA inputs
    msa_inputs = []
    for job in jobs:
        args = parser.parse_args(job)
        env, seqs, msa_track_id_sets, score_matrices = create_msa_input(job,
                                                                        args,
                                                                        manager,
                                                                        root_node)
        msa_inputs.append((args, env, seqs, msa_track_id_sets, score_matrices))
    end = time.time()
    print "Reading files took : " + (str(end - start)) + " seconds"
    # Do multiple sequence alignment from preprofile-annotated sequences.
    alignments = do_multiple_sequence_alignments(manager, root_node, msa_inputs)

    start = time.time()
    for msa_input, alignment in zip(msa_inputs, alignments):
        args, env, seqs, msa_track_id_sets, score_matrices = msa_input

        # Write alignment to output file.
        outfmt = args.output_format
        if outfmt == 'fasta':
            write_alignment_fasta(args.output, alignment, TRACK_ID_INPUT)
        elif outfmt == "clustal":
            write_alignment_clustal(args.output, alignment, TRACK_ID_INPUT,
                                    score_matrix)
        else:
            raise DataError("unknown output format: '{0}'".format(outfmt))

        # Dump pickled alignment object if user asked for it.
        if args.dump_alignment is not None:
            with file(args.dump_alignment, 'wb') as fo:
                pickle.dump(alignment, fo)

        if args.dump_all_tracks is not None:
            try:
                os.mkdir(args.dump_all_tracks)
            except OSError:
                pass

            all_trids = []
            for trid, track in alignment.items[0].tracks:
                if track.tid == PlainTrack.tid:
                    all_trids.append(trid)

            for trid in all_trids:
                filename = "dump-{0}.aln".format(trid)
                path = os.path.join(args.dump_all_tracks, filename)

                if outfmt == "fasta":
                    write_alignment_fasta(path, alignment, trid)
                elif outfmt == "clustal":
                    write_alignment_clustal(path, alignment, trid, None)
                else:
                    raise DataError("unknown output format: '{0}'".format(outfmt))
    end = time.time()
    print "Writing files took : " + (str(end - start)) + " seconds"
    # Collect log bundles
    if args.debug > 0:
        write_log_structure(root_node)



def create_msa_input(job, args, manager, root_node):
    verbose = False
    alphabet = ALPHABET_DNA

    seqs = load_sequence_fasta(args.input, alphabet)

    # Load inputs and other data.
    if args.score_matrix is not None:
        score_matrix_file = args.score_matrix
    else:
        score_matrix_file = 'nucleotide'

    # Read score parameters.
    with open_resource(score_matrix_file, "matrices") as f:
        score_matrices = [load_score_matrix(f, alphabet=alphabet)]
    gap_series = [-float(x) for x in args.gap_penalties.split(",")]

    # Setup environment.
    keys = {}
    keys['gap_series'] = gap_series
    keys['debug'] = args.debug
    keys['merge_mode'] = 'global'
    keys['dist_mode'] = 'global'
    keys['accelerate'] = True
    env = Environment(keys=keys)

    # Initialize root node for output
    root_node = TaskNode(ROOT_TAG)

    # Annotate the motifs from the files and patterns.
    track_scores = do_motif_annotation(args, env, manager, seqs,
                                             verbose, root_node)
    # Build score matrices.
    motif_score_matrices = {}
    for trid, score in track_scores.iteritems():
        if score is None:
            score = args.motif_match_score

        motif_score_matrices[trid] = get_motif_score_matrix(score,
                                                            args.score_spacers)

    # Add all the new annotation tracks to the list of tracks to use
    # in the alignment.
    track_id_sets = [[TRACK_ID_INPUT]]
    for trid, track in seqs[0].tracks:
        if trid in motif_score_matrices:
            track_id_sets.append([trid])
            score_matrices.append(motif_score_matrices[trid])

    # Build initial sets of which sequences to align against every master
    # sequence. By default, we want to align every input sequence against
    # every other input sequence.
    master_slave_seqs = []
    all_seqs = list(seqs)
    for master_seq in seqs:
        slave_seqs = []
        for slave_seq in seqs:
            if slave_seq is not master_seq:
                slave_seqs.append(slave_seq)
        master_slave_seqs.append((master_seq, slave_seqs))

    master_slave_alignments = do_master_slave_alignments(args, env,
                                                         manager,
                                                         master_slave_seqs,
                                                         track_id_sets,
                                                         score_matrices,
                                                         verbose,
                                                         root_node)

    # Build preprofiles from master-slave alignments.
    do_preprofiles(args, env, manager, master_slave_alignments, seqs,
                   verbose, root_node)
    msa_track_id_sets = _replace_input_track_id(track_id_sets)

    return env, seqs, msa_track_id_sets, score_matrices


def _replace_input_track_id(track_id_sets):
    new_track_id_sets = []
    for s in track_id_sets:
        new_s = []
        for tid in s:
            if tid == TRACK_ID_INPUT:
                new_s.append(TRACK_ID_PREPROFILE)
            else:
                new_s.append(tid)
        new_track_id_sets.append(new_s)
    return new_track_id_sets

def get_motif_score_matrix(match_score, score_spacers):
    all_symbols = {u"*", u"M", u"S"}

    d = {}
    for sym_one in all_symbols:
        for sym_two in all_symbols:
            if sym_one == u"M" and sym_two == u"M":
                score = match_score
            elif score_spacers and (sym_one == u"S" and sym_two == u"S"):
                score = match_score
            else:
                score = 0.0
            d[(sym_one, sym_two)] = score

    return ScoreMatrix(d, [ALPHABET_PROSITE, ALPHABET_PROSITE])


def do_motif_annotation(args, env, manager, seqs, verbose, root_node):
    FMT_TRACK_ID = "{0}_{1}"

    track_scores = {}

    execution = Execution(manager, ROOT_TAG)
    seq_patterns = []
    for seq in seqs:
        for pair in args.patterns:
            pattern = pair[0]
            if len(pair) > 1:
                score = float(pair[1])
            else:
                score = None
            seq_patterns.append((seq, pattern, score))

            component = PrositePatternAnnotator
            task = execution.add_task(component)
            task.environment(env)
            task.inputs(sequence=seq, pattern=pattern,
                        track_id=TRACK_ID_INPUT)

    outputs = run(execution, verbose=verbose, root_node=root_node)
    for n, output in enumerate(outputs):
        seq, pattern, score = seq_patterns[n]

        track = output['prediction_track']

        trid = FMT_TRACK_ID.format(_TRACK_ID_BASE_PATTERN, pattern)
        seq.add_track(trid, track)
        track_scores[trid] = score

    for pair in args.annotation_files:
        annotation_file = pair[0]
        if len(pair) > 1:
            score = float(pair[1])
        else:
            score = None

        annotation_seqs = load_sequence_fasta(annotation_file,
                                              ALPHABET_PROSITE)
        name_tracks = {}
        for annotation_seq in annotation_seqs:
            track = annotation_seq.get_track(TRACK_ID_INPUT)
            name_tracks[annotation_seq.name] = track

        for seq in seqs:
            track = name_tracks[seq.name]

            trid = FMT_TRACK_ID.format(_TRACK_ID_BASE_FILE, annotation_file)
            seq.add_track(trid, track)
            track_scores[trid] = score

    return track_scores

def do_master_slave_alignments(args, env, manager, seqs,
                               track_id_sets, score_matrices, verbose,
                               root_node):
    execution = Execution(manager, ROOT_TAG)

    master_slave_alignments = [None for seq in seqs]
    for master_seq, slave_seqs in seqs:
        component = DummyMasterSlaveAligner

        task = execution.add_task(component)
        task.environment(env)
        task.inputs(master_sequence=master_seq, slave_sequences=slave_seqs,
                    track_id_sets=track_id_sets, score_matrices=score_matrices)

    outputs = run(execution, verbose=verbose, root_node=root_node)
    for n, output in enumerate(outputs):
        master_slave_alignments[n] = output['alignment']

    return master_slave_alignments


def do_multiple_sequence_alignments(manager, root_node, msa_inputs):
    start = time.time()
    msa_execution = Execution(manager, ROOT_TAG)
    for msa_input in msa_inputs:
        args, env, seqs, track_id_sets, score_matrices = msa_input
        sub_env = Environment(parent=env)
        sub_env.keys['squash_profiles'] = True

        if args.tree_file is None:
            # Build guide tree
            component = GuideTreeBuilder
            execution = Execution(manager, ROOT_TAG)
            task = execution.add_task(component)
            task.environment(sub_env)
            task.inputs(sequences=seqs, track_id_sets=track_id_sets,
                        score_matrices=score_matrices)

            outputs = run(execution, verbose=False, root_node=root_node)[0]
            guide_tree = outputs['guide_tree']
        else:
            # Read guide tree and convert it into PRALINE format
            with open_resource(args.tree_file, 'trees') as f:
                tree = get_tree(f.read())

            labels = [get_name(seq.name) for seq in seqs]
            d = np.zeros((len(labels), len(labels)), dtype=np.float32)
            for n, label_one in enumerate(labels):
                for m, label_two in enumerate(labels):
                    if n == m:
                        continue
                    d[n, m] = tree_distance(tree, label_one, label_two)

            hc = HierarchicalClusteringAlgorithm(d)
            guide_tree = SequenceTree(seqs, list(hc.merge_order('average')))

        # Build MSA
        component = TreeMultipleSequenceAligner
        task = msa_execution.add_task(component)
        task.environment(env)
        task.inputs(sequences=seqs, guide_tree=guide_tree,
                    track_id_sets=track_id_sets, score_matrices=score_matrices)
    end = time.time()
    print "Preparing : " + (str(end - start)) + " seconds"
    outputs = run(msa_execution, verbose=False, root_node=root_node)
    alignments = [o['alignment'] for o in outputs]

    return alignments

def get_name_colon(seq):
    return seq.split(':')[0].lower()


def get_name_space(seq):
    return seq.split(' ')[0].lower().replace('_', ' ')

def get_name(seq):
    if seq.find(':') >= 0:
        return get_name_colon(seq)
    else:
        return get_name_space(seq)

def do_preprofiles(args, env, manager, alignments, seqs, verbose, root_node):
    for i, alignment in enumerate(alignments):
        component = ProfileBuilder
        execution = Execution(manager, ROOT_TAG)
        task = execution.add_task(component)
        task.environment(env)
        task.inputs(alignment=alignment, track_id=TRACK_ID_INPUT)

        outputs = run(execution, verbose=verbose, root_node=root_node)[0]
        track = outputs['profile_track']
        seqs[i].add_track(TRACK_ID_PREPROFILE, track)

def pair(arg):
    return arg.split(":")

def args_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="input file in FASTA format")
    parser.add_argument("output", help="output alignment")
    parser.add_argument("-g", "--gap-penalties",
                        help="comma separated list of positive gap penaties",
                        default="11,1", dest="gap_penalties")
    parser.add_argument("--motif-match-score",
                        default=1.0, dest="motif_match_score", type=float,
                        help="on matching motif status, boost score by " \
                             "this amount")
    parser.add_argument("--spacers-as-match",
                        default=False, action="store_true",  dest="score_spacers",
                        help="treat spacers as real matches for the scoring")
    parser.add_argument("-m", "--score-matrix",
                        help="score matrix to use for alignment",
                        default=None, dest="score_matrix")
    parser.add_argument("-s", "--preprofile-score", default=None,
                        dest="preprofile_score", type=float,
                        help="exclude preprofile alignments by score")
    parser.add_argument("-f", "--output-format", default="fasta",
                        dest="output_format",
                        help="write the alignment in the specified format")
    parser.add_argument("--debug", "-d", action="count", dest="debug",
                        default=0, help="enable debugging output")
    parser.add_argument("--dump-alignment-obj", type=str, default=None,
                        dest="dump_alignment",
                        help="dump final alignment object to filename")
    parser.add_argument("--dump-all-tracks", type=str, default=None,
                        dest="dump_all_tracks",
                        help="write alignment files for all the tracks")
    parser.add_argument('-p', '--pattern', action='append', dest="patterns",
                    required=True, type=pair,
                    help="annotate this prosite pattern in the sequence " \
                         "(specify a number after a colon to override " \
                         "the global match boost score)")
    parser.add_argument('-a', '--annotation-file', action='append',
                    type=pair, dest="annotation_files", default=[],
                    help="read motif annotation tracks from a FASTA file " \
                         "(specify a number after a colon to override " \
                         "the global match boost score)")
    parser.add_argument("--tree-file", default=None,
                        help="read the tree defining the join order from a " \
                             "file (in newick format)")

    return parser

def open_resource(filename, prefix):
    try:
        return file(filename)
    except IOError:
        return open_builtin(os.path.join(prefix, filename))

if __name__ == '__main__':
    main()
