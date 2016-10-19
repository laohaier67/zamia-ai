#!/bin/bash

DISTDIR=data/dist

rm -rf $DISTDIR
mkdir $DISTDIR

datum=`date +%Y%m%d`

#
# sphinx model de
#

AMNAME="cmusphinx-voxforge-de-r$datum"

mkdir "$DISTDIR/$AMNAME"
mkdir "$DISTDIR/$AMNAME/model_parameters"

cp -r data/dst/speech/de/cmusphinx/model_parameters/voxforge.cd_cont_3000 "$DISTDIR/$AMNAME/model_parameters"
cp -r data/dst/speech/de/cmusphinx/etc "$DISTDIR/$AMNAME"
cp data/dst/speech/de/cmusphinx/voxforge.html "$DISTDIR/$AMNAME"
cp README.md "$DISTDIR/$AMNAME"
cp COPYING   "$DISTDIR/$AMNAME"
cp AUTHORS   "$DISTDIR/$AMNAME"

pushd $DISTDIR
tar cfvz "$AMNAME.tgz" $AMNAME
popd

rm -r "$DISTDIR/$AMNAME"

#
# kaldi models de
#

AMNAME="kaldi-voxforge-de-r$datum"

mkdir "$DISTDIR/$AMNAME"

#     EXPNAME=tri2b_mmi_b0.05
#     GRAPHNAME=tri2b_denlats/dengraph

function export_kaldi_model {

    EXPNAME=$1
    GRAPHNAME=$2

    mkdir "$DISTDIR/$AMNAME/$EXPNAME"

    cp data/dst/speech/de/kaldi/exp/$EXPNAME/final.mdl   $DISTDIR/$AMNAME/$EXPNAME/model.mdl
    cp data/dst/speech/de/kaldi/exp/$EXPNAME/final.mat   $DISTDIR/$AMNAME/$EXPNAME/model.mat
    cp data/dst/speech/de/kaldi/exp/$EXPNAME/splice_opts $DISTDIR/$AMNAME/$EXPNAME/          2>/dev/null
    cp data/dst/speech/de/kaldi/exp/$EXPNAME/cmvn_opts   $DISTDIR/$AMNAME/$EXPNAME/          2>/dev/null 
    cp data/dst/speech/de/kaldi/exp/$EXPNAME/delta_opts  $DISTDIR/$AMNAME/$EXPNAME/          2>/dev/null 

    cp data/dst/speech/de/kaldi/exp/$GRAPHNAME/HCLG.fst  $DISTDIR/$AMNAME/$EXPNAME/
    cp data/dst/speech/de/kaldi/exp/$GRAPHNAME/words.txt $DISTDIR/$AMNAME/$EXPNAME/
    cp data/dst/speech/de/kaldi/exp/$GRAPHNAME/num_pdfs  $DISTDIR/$AMNAME/$EXPNAME/

}

# export_kaldi_model tri2b           tri2b/graph
export_kaldi_model tri2b_mmi       tri2b_denlats/dengraph
export_kaldi_model tri2b_mmi_b0.05 tri2b_denlats/dengraph
export_kaldi_model tri2b_mpe       tri2b_denlats/dengraph
# export_kaldi_model tri3b           tri3b/graph
export_kaldi_model tri3b_mpe       tri3b_denlats/dengraph
export_kaldi_model tri3b_mmi       tri3b_denlats/dengraph
export_kaldi_model tri3b_mmi_b0.05 tri3b_denlats/dengraph

cp data/dst/speech/de/kaldi/RESULTS.txt $DISTDIR/$AMNAME/
cp README.md "$DISTDIR/$AMNAME"
cp COPYING   "$DISTDIR/$AMNAME"
cp AUTHORS   "$DISTDIR/$AMNAME"

mkdir "$DISTDIR/$AMNAME/conf"
cp data/src/speech/kaldi-mfcc.conf        $DISTDIR/$AMNAME/conf/mfcc.conf 
cp data/src/speech/kaldi-mfcc-hires.conf  $DISTDIR/$AMNAME/conf/mfcc-hires.conf  
cp data/src/speech/kaldi-online-cmvn.conf $DISTDIR/$AMNAME/conf/online_cmvn.conf

pushd $DISTDIR
tar cfvz "$AMNAME.tgz" $AMNAME
popd

rm -r "$DISTDIR/$AMNAME"

#
# srilm de
#

LMNAME="srilm-voxforge-de-r$datum"
cp data/dst/speech/de/kaldi/data/local/lm/lm.arpa data/dist/$LMNAME
gzip data/dist/$LMNAME

# 
# cmuclmtk de
#

LMNAME="cmuclmtk-voxforge-de-r$datum"
cp data/dst/speech/de/cmusphinx/voxforge.arpa $DISTDIR/$LMNAME
gzip $DISTDIR/$LMNAME

#
# sequitur de
#

MODELNAME="sequitur-voxforge-de-r$datum"
cp data/dst/speech/de/sequitur/model-6 $DISTDIR/$MODELNAME
gzip $DISTDIR/$MODELNAME

#
# copyright info
#

cp README.md "$DISTDIR"
cp COPYING   "$DISTDIR"
cp AUTHORS   "$DISTDIR"

#
# upload
#

echo rsync -avPz --delete --bwlimit=256 data/dist goofy:/var/www/html/voxforge/de
