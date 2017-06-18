#!/usr/bin/env python
# -*- coding: utf-8 -*- 

#
# Copyright 2015, 2016, 2017 Guenter Bartsch
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# ai kernal, central hub for all the other components to hook into
#
# natural language -> [ tokenizer ] -> tokens -> [ seq2seq model ] -> prolog -> [ prolog engine ] -> say/action preds
#

import os
import sys
import logging
import traceback
import imp
import time
import random
import codecs
import rdflib
import datetime
import pytz

import numpy as np

from tzlocal              import get_localzone # $ pip install tzlocal
from copy                 import deepcopy
from sqlalchemy.orm       import sessionmaker

import model

from zamiaprolog.logicdb  import LogicDB, LogicDBOverlay
from zamiaprolog.logic    import StringLiteral, ListLiteral, NumberLiteral, SourceLocation, json_to_prolog, prolog_to_json, Predicate, Clause, Literal
from zamiaprolog.errors   import PrologError
from zamiaprolog.builtins import ASSERT_OVERLAY_VAR_NAME, do_gensym
from zamiaprolog.parser   import PrologParser
from aiprolog.pl2rdf      import pl_literal_to_rdf
from aiprolog.runtime     import AIPrologRuntime, CONTEXT_GRAPH_NAME, USER_PREFIX, CURIN, KB_PREFIX, DEFAULT_USER

from kb                   import AIKB
from nltools              import misc
from nltools.tokenizer    import tokenize

# FIXME: current audio model tends to insert 'hal' at the beginning of utterances:
ENABLE_HAL_PREFIX_HACK = True

TEST_USER          = USER_PREFIX + u'test'
TEST_TIME          = datetime.datetime(2016,12,06,13,28,6,tzinfo=get_localzone()).isoformat()
TEST_MODULE        = '__test__'

NUM_CONTEXT_ROUNDS = 3

class AIKernal(object):

    def __init__(self):

        self.config = misc.load_config('.airc')

        #
        # database
        #

        Session = sessionmaker(bind=model.engine)
        self.session = Session()

        #
        # logic DB
        #

        self.db = LogicDB(model.url)

        #
        # knowledge base
        #

        self.kb = AIKB()

        #
        # TensorFlow (deferred, as tf can take quite a bit of time to set up)
        #

        self.tf_session = None
        self.nlp_model  = None

        #
        # module management, setup
        #

        self.modules             = {}
        self.initialized_modules = set()
        s = self.config.get('semantics', 'modules')
        self.all_modules         = map (lambda s: s.strip(), s.split(','))

        #
        # prolog environment setup
        #

        self.prolog_rt = AIPrologRuntime(self.db, self.kb)
        self.parser    = PrologParser ()


    # FIXME: this will work only on the first call
    def setup_tf_model (self, forward_only, load_model, ini_fn):

        if not self.tf_session:

            import tensorflow as tf

            # setup config to use BFC allocator
            config = tf.ConfigProto()  
            config.gpu_options.allocator_type = 'BFC'

            self.tf_session = tf.Session(config=config)

        if not self.nlp_model:

            from nlp_model import NLPModel

            self.nlp_model = NLPModel(self.session, ini_fn)

            if load_model:

                self.nlp_model.load_dicts()

                # we need the inverse dict to reconstruct the output from tensor

                self.inv_output_dict = {v: k for k, v in self.nlp_model.output_dict.iteritems()}

                self.tf_model = self.nlp_model.create_tf_model(self.tf_session, forward_only = forward_only) 
                self.tf_model.batch_size = 1

                self.nlp_model.load_model(self.tf_session)


    def clean (self, module_names, clean_all, clean_logic, clean_discourses, 
                                   clean_cronjobs, clean_kb):

        for module_name in module_names:

            if clean_logic or clean_all:
                logging.info('cleaning logic for %s...' % module_name)
                if module_name == 'all':
                    self.db.clear_all_modules()
                else:
                    self.db.clear_module(module_name)

            if clean_discourses or clean_all:
                logging.info('cleaning discourses for %s...' % module_name)
                if module_name == 'all':
                    self.session.query(model.DiscourseRound).delete()
                else:
                    self.session.query(model.DiscourseRound).filter(model.DiscourseRound.module==module_name).delete()

            if clean_cronjobs or clean_all:
                logging.info('cleaning cronjobs for %s...' % module_name)
                if module_name == 'all':
                    self.session.query(model.Cronjob).delete()
                else:
                    self.session.query(model.Cronjob).filter(model.Cronjob.module==module_name).delete()

            if clean_kb or clean_all:
                logging.info('cleaning kb for %s...' % module_name)
                if module_name == 'all':
                    self.kb.clear_all_graphs()
                else:
                    graph = self._module_graph_name(module_name)
                    self.kb.clear_graph(graph)

        self.session.commit()

    def load_module (self, module_name):

        # import pdb; pdb.set_trace()
        if module_name in self.modules:
            return self.modules[module_name]

        logging.debug("loading module '%s'" % module_name)

        fp, pathname, description = imp.find_module(module_name, ['modules'])

        # print fp, pathname, description

        m = None

        try:
            m = imp.load_module(module_name, fp, pathname, description)

            self.modules[module_name] = m

            # print m
            # print getattr(m, '__all__', None)

            # for name in dir(m):
            #     print name

            for m2 in getattr (m, 'DEPENDS'):
                self.load_module(m2)

            if hasattr(m, 'RDF_PREFIXES'):
                prefixes = getattr(m, 'RDF_PREFIXES')
                for prefix in prefixes:
                    self.kb.register_prefix(prefix, prefixes[prefix])

            if hasattr(m, 'LDF_ENDPOINTS'):
                endpoints = getattr(m, 'LDF_ENDPOINTS')
                for endpoint in endpoints:
                    self.kb.register_endpoint(endpoint, endpoints[endpoint])

            if hasattr(m, 'RDF_ALIASES'):
                aliases = getattr(m, 'RDF_ALIASES')
                for alias in aliases:
                    self.kb.register_alias(alias, aliases[alias])

            if hasattr(m, 'CRONJOBS'):

                # update cronjobs in db

                old_cronjobs = set()
                for cronjob in self.session.query(model.Cronjob).filter(model.Cronjob.module==module_name):
                    old_cronjobs.add(cronjob.name)

                new_cronjobs = set()
                for name, interval, f in getattr (m, 'CRONJOBS'):

                    logging.debug ('registering cronjob %s' %name)

                    cj = self.session.query(model.Cronjob).filter(model.Cronjob.module==module_name, model.Cronjob.name==name).first()
                    if not cj:
                        cj = model.Cronjob(module=module_name, name=name, last_run=0)
                        self.session.add(cj)

                    cj.interval = interval
                    new_cronjobs.add(cj.name)

                for cjn in old_cronjobs:
                    if cjn in new_cronjobs:
                        continue
                    self.session.query(model.Cronjob).filter(model.Cronjob.module==module_name, model.Cronjob.name==cjn).delete()

                self.session.commit()

            if hasattr(m, 'init_module'):
                initializer = getattr(m, 'init_module')
                initializer(self.prolog_rt)

        except:
            logging.error(traceback.format_exc())

        finally:
            # Since we may exit via an exception, close fp explicitly.
            if fp:
                fp.close()

        return m

    def init_module (self, module_name, run_trace=False):

        # import pdb; pdb.set_trace()
        if module_name in self.initialized_modules:
            return

        logging.debug("initializing module '%s'" % module_name)

        self.initialized_modules.add(module_name)

        m = self.load_module(module_name)

        for m2 in getattr (m, 'DEPENDS'):
            self.init_module(m2, run_trace=run_trace)

        gn = rdflib.Graph(identifier=CONTEXT_GRAPH_NAME)
        self.kb.remove((CURIN, None, None, gn))

        quads = [ ( CURIN, KB_PREFIX+u'user', DEFAULT_USER, gn) ]

        self.kb.addN_resolve(quads)

        prolog_s = u'init(\'%s\')' % (module_name)
        c = self.parser.parse_line_clause_body(prolog_s)

        self.prolog_rt.set_trace(run_trace)

        solutions = self.prolog_rt.search(c)

    def _module_graph_name (self, module_name):
        return KB_PREFIX + module_name

    def _p2e_mapper(self, p):
        if p.startswith('http://www.wikidata.org/prop/direct/'):
            return 'http://www.wikidata.org/entity/' + p[36:]
        if p.startswith('http://www.wikidata.org/prop/'):
            return 'http://www.wikidata.org/entity/' + p[29:]
        return None

    def import_kb (self, module_name):

        graph = self._module_graph_name(module_name)

        self.kb.register_graph(graph)

        # disabled to enable incremental kb updates self.kb.clear_graph(graph)

        m = self.modules[module_name]

        # import LDF first as it is incremental

        res_paths = []
        for kb_entry in getattr (m, 'KB_SOURCES'):
            if not isinstance(kb_entry, basestring):
                res_paths.append(kb_entry)

        if len(res_paths)>0:
            logging.info('mirroring from LDF endpoints, target graph: %s ...' % graph)
            quads = self.kb.ldf_mirror(res_paths, graph, self._p2e_mapper)

        # now import files, if any

        for kb_entry in getattr (m, 'KB_SOURCES'):
            if isinstance(kb_entry, basestring):
                kb_pathname = 'modules/%s/%s' % (module_name, kb_entry)
                logging.info('importing %s ...' % kb_pathname)
                self.kb.parse_file(graph, 'n3', kb_pathname)


    def import_kb_multi (self, module_names):

        for module_name in module_names:

            if module_name == 'all':

                for mn2 in self.all_modules:
                    self.load_module (mn2)
                    self.import_kb (mn2)

            else:

                self.load_module (module_name)

                self.import_kb (module_name)

        self.session.commit()

    _CONTEXT_IGNORE_IAS_KEYS = set([ 'user', 'uttLang', 'tokens', 'currentTime', 'prevIAS' ])

    def _compute_net_input (self, env, cur_ias, location):

        context = []
        for r in range(NUM_CONTEXT_ROUNDS):

            s4s = self.prolog_rt.search_predicate ('ias', [cur_ias, 'K', 'V'], env=env, location=location, err_on_missing=True)
           
            prev_ias = None
            tokens   = None

            d = {}

            for s4 in s4s:

                k = s4['K']
                v = s4['V']

                if not isinstance(k, Predicate):
                    continue

                if k.name == 'prevIAS':
                    prev_ias = v.name

                if k.name == 'tokens':
                    tokens = v.l

                if k.name in self._CONTEXT_IGNORE_IAS_KEYS:
                    continue

                d[k.name] = v

            for t in reversed(tokens):
                context.insert(0, t.s)
            for k in sorted(d):
                context.insert(0, d[k])
                context.insert(0, k)

            if not prev_ias:
                break
            cur_ias = prev_ias

        return context

    def _setup_ias (self, sl, test_mode, user_uri, utterance, utt_lang, tokens, prev_ias, prev_ovl):

        cur_ias = Predicate(do_gensym(self.prolog_rt, 'ias'))

        ovl = prev_ovl.clone() if prev_ovl else LogicDBOverlay()
        env = {
               'I'                     : cur_ias,
               ASSERT_OVERLAY_VAR_NAME : ovl
              }

        if not prev_ias:
            # find prev_ias for this user, if any
            # FIXME: there should be a more efficient way than linear search

            prev_ias = None
            for s in self.prolog_rt.search_predicate('ias', ['I', 'user', StringLiteral(user_uri)], env=env, err_on_missing=False):

                ias = s['I']

                if not prev_ias:
                    prev_ias = ias
                    continue

                if ias.name > prev_ias.name:
                    prev_ias = ias

        ovl.assertz(Clause(Predicate('ias', [cur_ias, Predicate('user'),        StringLiteral(user_uri)]),  location=sl))
        ovl.assertz(Clause(Predicate('ias', [cur_ias, Predicate('uttLang'),     Predicate(name=utt_lang)]), location=sl))
        ovl.assertz(Clause(Predicate('ias', [cur_ias, Predicate('tokens'),      ListLiteral(tokens)]),      location=sl))
        currentTime = StringLiteral(datetime.datetime.now().replace(tzinfo=pytz.UTC).isoformat())
        ovl.assertz(Clause(Predicate('ias', [cur_ias, Predicate('currentTime'), currentTime]),              location=sl))

        if prev_ias:
            ovl.assertz(Clause(Predicate('ias', [cur_ias, Predicate('prevIAS'), prev_ias]), location=sl))

            # copy over all previous statements to the new one

            s4s = self.prolog_rt.search_predicate ('ias', [prev_ias, 'K', 'V'], location=sl, env=env, err_on_missing=False)
            for s4 in s4s:
                k = s4['K']
                if not isinstance(k, Predicate):
                    continue
                if k.name in self._CONTEXT_IGNORE_IAS_KEYS:
                    continue
                ovl.assertz(Clause(Predicate('ias', [cur_ias, Predicate(k.name), s4['V']]), location=sl))

        return cur_ias, env

    def compile_module (self, module_name, run_trace=False, print_utterances=False):

        m = self.modules[module_name]

        logging.debug('parsing sources of module %s (print_utterances: %s) ...' % (module_name, print_utterances))

        compiler = PrologParser ()

        compiler.clear_module(module_name, self.db)

        for pl_fn in getattr (m, 'PL_SOURCES'):
            
            pl_pathname = 'modules/%s/%s' % (module_name, pl_fn)

            logging.debug('   parsing %s ...' % pl_pathname)
            compiler.compile_file (pl_pathname, module_name, self.db, clear_module=False)

        # delete old NLP training data

        self.session.query(model.TrainingData).filter(model.TrainingData.module==module_name).delete()

        # extract NLP training data

        sl = SourceLocation('<input>', 0, 0)
        solutions = self.prolog_rt.search_predicate ('nlp_train', [StringLiteral(module_name), 'LANG', 'DATA'], env={}, location=sl, err_on_missing=False)

        todo = []
        for solution in solutions:

            utt_lang  = solution['LANG'].name

            data = solution['DATA'].l

            if len(data) % 4 != 0:
                raise PrologError ('Error: training data length has to be multiple of 4!', sl)

            data_pos = 0

            todo.append((data, data_pos, None, {}))

        # now: simulate all conversations to extract context training information

        self.prolog_rt.db.clear_module(TEST_MODULE)

        training_data_cnt = 0
        logging.info ('module %s training data extraction...' % module_name)

        while len(todo)>0:

            data, data_pos, prev_ias, prev_ovl = todo.pop()
            if data_pos >= len(data):
                continue

            prep      = data[data_pos].l
            tokens    = data[data_pos+1].l
            gcode     = data[data_pos+2].l
            rcode     = data[data_pos+3].l
            tokenss   = map(lambda s: s.s, tokens)
            utterance = u' '.join(tokenss)

            data_pos += 4

            cur_ias, env = self._setup_ias (sl, test_mode = True, 
                                                user_uri  = TEST_USER, 
                                                utterance = utterance, 
                                                utt_lang  = utt_lang, 
                                                tokens    = tokens,
                                                prev_ias  = prev_ias,
                                                prev_ovl  = prev_ovl)

            if prep:
                p = Clause (body=Predicate(name='and', args=prep), location=sl)
                solutions = self.prolog_rt.search(p, env=env)
                if len(solutions) != 1:
                    raise PrologError("Expected exactly one solution when running the preparation code, got %d" % len(solutions), sl)
                env = solutions[0]

            inp = self._compute_net_input (env, cur_ias, sl)

            if print_utterances:
                logging.info (u'utterance  : %s' % unicode(utterance))
                logging.info (u'layer 0 inp: %s' % repr(inp))

            found     = False
            inp_json  = prolog_to_json(inp)
            resp_json = prolog_to_json(gcode)
            for tdr in self.session.query(model.TrainingData).filter(model.TrainingData.lang  == utt_lang,
                                                                     model.TrainingData.layer == 0,
                                                                     model.TrainingData.inp   == inp_json):
                if tdr.resp == resp_json:
                    found = True
                    break

            if not found:
                self.session.add(model.TrainingData(lang      = utt_lang,
                                                    module    = module_name,
                                                    layer     = 0,
                                                    utterance = utterance,
                                                    inp       = inp_json,
                                                    resp      = resp_json))
                training_data_cnt += 1
                if training_data_cnt % 100 == 0:
                    logging.info ('...module %s training data cnt: %d' %(module_name, training_data_cnt))
            else:
                logging.debug ('layer 0 tdr for "%s" already in DB' % utterance)
            
            c2 = Clause (body=Predicate(name='and', args=gcode), location=sl)
            s2s = self.prolog_rt.search(c2, env=env)

            for s2 in s2s:

                todo.append((data, data_pos, cur_ias, s2[ASSERT_OVERLAY_VAR_NAME]))

                # logging.info ('s2: %s' % repr(s2))
                    
                inp = self._compute_net_input (s2, cur_ias, sl)

                if print_utterances:
                    logging.info (u'layer 1 inp: %s' % repr(inp))
                    logging.info (u'layer 1 res: %s' % repr(rcode))

                found     = False
                inp_json  = prolog_to_json(inp)
                resp_json = prolog_to_json(rcode)

                for tdr in self.session.query(model.TrainingData).filter(model.TrainingData.lang  == utt_lang,
                                                                         model.TrainingData.layer == 1,
                                                                         model.TrainingData.inp   == inp_json):

                    if tdr.resp == resp_json:
                        found = True
                        break

                if not found:
                    self.session.add(model.TrainingData(lang      = utt_lang,
                                                        module    = module_name,
                                                        layer     = 1,
                                                        utterance = utterance,
                                                        inp       = inp_json,
                                                        resp      = resp_json))
                    training_data_cnt += 1
                    if training_data_cnt % 100 == 0:
                        logging.info ('...module %s training data cnt: %d' %(module_name, training_data_cnt))
                else:
                    logging.debug ('layer 1 tdr for "%s" already in DB' % utterance)

        logging.info ('module %s training data extraction done. total cnt: %d' %(module_name, training_data_cnt))

        # if self.discourse_rounds:

        #     # logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

        #     start_time = time()
        #     logging.info (u'bulk saving %d discourse rounds to db...' % len(self.discourse_rounds))
        #     self.db.session.bulk_save_objects(self.discourse_rounds)
        #     self.db.commit()
        #     logging.info (u'bulk saving %d discourse rounds to db... done. Took %fs.' % (len(self.discourse_rounds), time()-start_time))

    def compile_module_multi (self, module_names, run_trace=False, print_utterances=False):

        for module_name in module_names:

            if module_name == 'all':

                for mn2 in self.all_modules:
                    self.load_module (mn2)
                    self.compile_module (mn2, run_trace, print_utterances)

            else:
                self.load_module (module_name)
                self.compile_module (module_name, run_trace, print_utterances)

        self.session.commit()

    def _extract_response (self, resp, cur_ias, env, sl):

        # prepare IAS value dict, sum up scores
        d     = {}
        score = 0.0
        for s in self.prolog_rt.search_predicate ('ias', [cur_ias, 'K', 'V'], env=env, location=sl, err_on_missing=True):
            
            k = s['K']
            v = s['V']

            if not isinstance(k, Predicate):
                continue

            if k.name == 'score':
                score += v.f
                continue

            if not isinstance(v, Literal):
                continue

            d[k.name] = unicode(v)

        utterance = u''
        utt_lang  = u'en'
        actions   = []

        for r in resp:
            if r.name == 'say':
                l          = r.args[0]
                word       = r.args[1]
                if len(utterance)>0:
                    utterance += u' '
                utterance += word.s
                utt_lang   = l.name

            elif r.name == 'sayv':
                if len(utterance)>0:
                    utterance += u' '

                vn = r.args[1].name
                fc = r.args[2].name
                if vn in d:
                    if fc == 'd':
                        utterance += unicode(int(round(float(d[vn]))))
                    else:
                        utterance += unicode(d[vn])
                else:
                    utterance += u'???'

                # utterance += d[vn] if vn in d else u'???'

            else:
                actions.append(r)
                               
        return utterance, utt_lang, actions, score


    def test_module (self, module_name, trace=False, test_name=None):

        logging.info('extracting tests of module %s ...' % (module_name))

        sl = SourceLocation('<input>', 0, 0)
        nlp_tests = self.prolog_rt.search_predicate ('nlp_test', [StringLiteral(module_name), 'LANG', 'NAME', 'PREP', 'DATA'], env={}, location=sl, err_on_missing=False)

        if len(nlp_tests)==0:
            logging.warn('module %s has no tests.' % module_name)
            return

        logging.info('running %d tests of module %s ...' % (len(nlp_tests), module_name))

        for nlp_test in nlp_tests:

            if test_name:
                name = nlp_test['NAME'].s
                if name != test_name:
                    logging.info ('skipping test %s' % name)
                    continue

            prep = nlp_test['PREP'].l
            data = nlp_test['DATA'].l
            if len(data) % 3 != 0:
                raise PrologError ('Error: test data length has to be multiple of 3!', sl)

            utt_lang  = nlp_test['LANG'].name
            context   = []
            prev_ias  = None
            prev_ovl  = {}
            round_num = 0

            while len(data)>round_num*3:

                test_in      = u' '.join(tokenize(data[round_num*3].s, lang=utt_lang))
                test_out     = u' '.join(tokenize(data[round_num*3+1].s, lang=utt_lang))
                test_actions = data[round_num*3+2].l

                logging.info("nlp_test: %s round %d test_in     : %s" % (sl, round_num, test_in) )
                logging.info("nlp_test: %s round %d test_out    : %s" % (sl, round_num, test_out) )
                logging.info("nlp_test: %s round %d test_actions: %s" % (sl, round_num, test_actions) )

                tokenss   = tokenize(test_in, utt_lang)
                tokens    = map (lambda t: StringLiteral(t), tokenss)

                cur_ias, env = self._setup_ias (sl, test_mode = True, 
                                                    user_uri  = TEST_USER, 
                                                    utterance = test_in, 
                                                    utt_lang  = utt_lang, 
                                                    tokens    = tokens,
                                                    prev_ias  = prev_ias,
                                                    prev_ovl  = prev_ovl)

                if prep:
                    p = Clause (body=Predicate(name='and', args=prep), location=sl)
                    solutions = self.prolog_rt.search(p, env=env)
                    if len(solutions) != 1:
                        raise PrologError("Expected exactly one solution when running the preparation code, got %d" % len(solutions), sl)
                    env = solutions[0]

                inp = self._compute_net_input (env, cur_ias, sl)

                # look up g-code in DB

                gcode = None
                for tdr in self.session.query(model.TrainingData).filter(model.TrainingData.lang  == utt_lang,
                                                                         model.TrainingData.layer == 0,
                                                                         model.TrainingData.inp   == prolog_to_json(inp)):
                    if gcode:
                        logging.warn (u'%s: layer 0 more than one gcode for test_in "%s" found in DB!' % (sl, test_in))

                    gcode = json_to_prolog (tdr.resp)

                if gcode is None:
                    raise PrologError (u'Error: layer 0 no training data for test_in "%s" found in DB!' % test_in, sl)
                    
                c2 = Clause (body=Predicate(name='and', args=gcode), location=sl)
                s2s = self.prolog_rt.search(c2, env=env)

                if len(s2s) == 0:
                    raise PrologError ('G code for utterance "%s" failed!' % test_in, sl)
                else:
                    logging.info("nlp_test: %s round %d got %s result(s) from g-code." % (sl, round_num, len(s2s)))

                for s2 in s2s:

                    # logging.info ('s2: %s' % repr(s2))

                    inp = self._compute_net_input (s2, cur_ias, sl)

                    # look up response in DB

                    response      = None
                    matching_resp = False

                    for tdr in self.session.query(model.TrainingData).filter(model.TrainingData.lang  == utt_lang,
                                                                             model.TrainingData.layer == 1,
                                                                             model.TrainingData.inp   == prolog_to_json(inp)):

                        response = json_to_prolog (tdr.resp)
                        actual_out, actual_lang, actual_actions, score = self._extract_response (response, cur_ias, s2, sl)

                        # logging.info("nlp_test: %s round %d %s" % (clause.location, round_num, repr(abuf)) )

                        if len(test_out) > 0:
                            if len(actual_out)>0:
                                actual_out = u' '.join(tokenize(actual_out, utt_lang))
                            logging.info("nlp_test: %s round %d actual_out  : %s (score: %f)" % (sl, round_num, actual_out, score) )
                            if actual_out != test_out:
                                logging.info("nlp_test: %s round %d UTTERANCE MISMATCH." % (sl, round_num))
                                continue # no match

                        logging.info("nlp_test: %s round %d UTTERANCE MATCHED!" % (sl, round_num))

                        # check actions

                        if len(test_actions)>0:

                            # print repr(test_actions)

                            actions_matched = True
                            for action in test_actions:
                                for act in actual_actions:
                                    # print "    check action match: %s vs %s" % (repr(action), repr(act))
                                    if action == act:
                                        break
                                if action != act:
                                    actions_matched = False
                                    break

                            if not actions_matched:
                                logging.info("nlp_test: %s round %d ACTIONS MISMATCH." % (sl, round_num))
                                continue

                            logging.info("nlp_test: %s round %d ACTIONS MATCHED!" % (sl, round_num))

                        matching_resp = True

                        prev_ias = cur_ias
                        prev_ovl = s2[ASSERT_OVERLAY_VAR_NAME]

                        break

                    if not response:
                        raise PrologError (u'Error: no layer1 training data for inp %s found in DB!' % repr(inp), sl)
                    
                    if not matching_resp:
                        raise PrologError (u'nlp_test: %s round %d no matching response found.' % (sl, round_num))
                       
                round_num += 1

    def run_tests_multi (self, module_names, run_trace=False, test_name=None):

        for module_name in module_names:

            if module_name == 'all':

                for mn2 in self.all_modules:
                    self.load_module (mn2)
                    self.init_module (mn2, run_trace=run_trace)
                    self.test_module (mn2, trace=run_trace, test_name=test_name)

            else:
                # import pdb; pdb.set_trace()
                self.load_module (module_name)
                self.init_module (module_name, run_trace=run_trace)
                self.test_module (module_name, trace=run_trace, test_name=test_name)

    # FIXME: old code, needs to be ported or removed
    def process_input (self, utterance, utt_lang, user_uri, test_mode=False, trace=False):

        """ process user input, return action(s) """

        gn = rdflib.Graph(identifier=CONTEXT_GRAPH_NAME)

        tokens = tokenize(utterance, utt_lang)

        if ENABLE_HAL_PREFIX_HACK:
            if tokens[0] == u'hal':
                del tokens[0]

        #
        # provide utterance related data via db overlay/environment
        #

        sl = SourceLocation('<input>', 0, 0)

        cur_ias, env = self._setup_ias(sl, test_mode, user_uri, utterance, utt_lang, tokens, None, {})

        self.prolog_rt.set_trace(trace)


        prolog_s = []
        if test_mode:

            for dr in self.db.session.query(model.DiscourseRound).filter(model.DiscourseRound.inp==utterance, 
                                                                         model.DiscourseRound.lang==utt_lang):
                prolog_s.append(u','.join(dr.resp.split(';')))

            logging.debug("test tokens=%s prolog_s=%s" % (repr(tokens), repr(prolog_s)) )
                
            if not prolog_s:
                logging.error('test utterance %s not found!' % utterance)
                return []

        else:

            x = self.nlp_model.compute_x(utterance)

            logging.debug("x: %s -> %s" % (utterance, x))

            # which bucket does it belong to?
            bucket_id = min([b for b in xrange(len(self.nlp_model.buckets)) if self.nlp_model.buckets[b][0] > len(x)])

            # get a 1-element batch to feed the sentence to the model
            encoder_inputs, decoder_inputs, target_weights = self.tf_model.get_batch( {bucket_id: [(x, [])]}, bucket_id )

            # print "encoder_inputs, decoder_inputs, target_weights", encoder_inputs, decoder_inputs, target_weights

            # get output logits for the sentence
            _, _, output_logits = self.tf_model.step(self.tf_session, encoder_inputs, decoder_inputs, target_weights, bucket_id, True)

            logging.debug("output_logits: %s" % repr(output_logits))

            # this is a greedy decoder - outputs are just argmaxes of output_logits.
            outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]

            # print "outputs", outputs

            preds = map (lambda o: self.inv_output_dict[o], outputs)
            logging.debug("preds: %s" % repr(preds))

            # FIXME: handle ;;
            prolog_s = ''

            do_and = True

            for p in preds:

                if p[0] == '_':
                    continue # skip _EOS

                if p == u'or':
                    do_and = False
                    continue

                if len(prolog_s)>0:
                    if do_and:
                        prolog_s += ', '
                    else:
                        prolog_s += '; '
                prolog_s += p

                do_and = True

            logging.debug('?- %s' % prolog_s)

        abufs = []

        for ps in prolog_s:

            c = self.parser.parse_line_clause_body(ps)
            # logging.debug( "Parse result: %s" % c)

            # logging.debug( "Searching for c: %s" % c )

            solutions = self.prolog_rt.search(c, env=env)

            # if len(solutions) == 0:
            #     raise PrologError ('nlp_test: %s no solution found.' % clause.location)

            # extract action buffers from overlay variable in solutions:

            for solution in solutions:

                overlay = solution.get(ASSERT_OVERLAY_VAR_NAME)
                if not overlay:
                    continue

                actions = []
                for s in self.prolog_rt.search_predicate('ias', [cur_ias, 'action', 'A'], env={ASSERT_OVERLAY_VAR_NAME: overlay}):
                    actions.append(s['A'])

                score = 0.0
                for s in self.prolog_rt.search_predicate('ias', [cur_ias, 'score', 'S'], env={ASSERT_OVERLAY_VAR_NAME: overlay}):
                    score += s['S'].f

                # ias = overlay.get('ias')

                # scores  = overlay.get('score')
                # score = reduce(lambda a,b: a+b, scores) if scores else 0.0
               
                abufs.append({'actions': actions, 'score': score, 'overlay': overlay})

        return abufs

    def do_eliza (self, utterance, utt_lang, trace=False):

        """ produce eliza-style response """

        logging.info ('producing ELIZA-style response for input %s' % utterance)

        self.prolog_rt.reset_actions()
        self.prolog_rt.set_trace(trace)

        c = self.parser.parse_line_clause_body('answer(dodge_question, %s)' % utt_lang)
        solutions = self.prolog_rt.search(c)
        abufs = self.prolog_rt.get_actions()

        return abufs


    def run_cronjobs (self, module_name, force=False):

        m = self.modules[module_name]
        if not hasattr(m, 'CRONJOBS'):
            return

        graph = self._module_graph_name(module_name)

        self.kb.register_graph(graph)

        for name, interval, f in getattr (m, 'CRONJOBS'):

            cronjob = self.session.query(model.Cronjob).filter(model.Cronjob.module==module_name, model.Cronjob.name==name).first()

            t = time.time()

            next_run = cronjob.last_run + interval

            if force or t > next_run:

                logging.debug ('running cronjob %s' %name)
                f (self.config, self.kb, graph)

                cronjob.last_run = t

    def run_cronjobs_multi (self, module_names, force, run_trace=False):

        for module_name in module_names:

            if module_name == 'all':

                for mn2 in self.all_modules:
                    self.load_module (mn2)
                    self.init_module (mn2, run_trace=run_trace)
                    self.run_cronjobs (mn2, force=force)

            else:
                self.load_module (module_name)
                self.init_module (module_name, run_trace=run_trace)
                self.run_cronjobs (module_name, force=force)

        self.session.commit()

    def train (self, ini_fn):

        self.setup_tf_model (False, False, ini_fn)
        self.nlp_model.train()


    def dump_utterances (self, num_utterances, dictfn, lang, module):

        dic = None
        if dictfn:
            dic = set()
            with codecs.open(dictfn, 'r', 'utf8') as dictf:
                for line in dictf:
                    parts = line.strip().split(';')
                    if len(parts) != 2:
                        continue
                    dic.add(parts[0])

        all_utterances = []

        req = self.session.query(model.DiscourseRound).filter(model.DiscourseRound.lang==lang)

        if module and module != 'all':
            req = req.filter(model.DiscourseRound.module==module)

        for dr in req:

            if not dic:
                all_utterances.append(dr.inp)
            else:

                # is at least one word not covered by our dictionary?

                unk = False
                for t in tokenize(dr.inp):
                    if not t in dic:
                        # print u"unknown word: %s in %s" % (t, dr.inp)
                        unk = True
                        dic.add(t)
                        break
                if not unk:
                    continue

                all_utterances.append(dr.inp)

        utts = set()

        if num_utterances > 0:

            while (len(utts) < num_utterances):

                i = random.randrange(0, len(all_utterances))
                utts.add(all_utterances[i])

        else:
            for utt in all_utterances:
                utts.add(utt)
                
        for utt in utts:
            print utt



