#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2020-2021 Barcelona Supercomputing Center (BSC), Spain
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import

import atexit
import hashlib
import http
import json
import os
import platform
import shutil
import subprocess
import tempfile
import types

from urllib import request, parse
from rocrate import rocrate

if platform.system() == "Darwin":
    import ssl

    ssl._create_default_https_context = ssl._create_unverified_context

from collections import namedtuple

MaterializedContent = namedtuple('MaterializedContent', ['local', 'uri', 'prettyFilename'])
MaterializedInput = namedtuple('MaterializedInput', ['name', 'values'])


class WFException(Exception):
    pass


def fetchClassicURL(remote_file, cachedFilename, secContext=None):
    """
    Method to fetch contents from http, https and ftp
    """
    try:
        if isinstance(secContext, dict):
            username = secContext.get('username')
            password = secContext.get('password')
            if username is not None:
                if password is None:
                    password = ''

                # Time to set up user and password in URL
                parsedInputURL = parse.urlparse(remote_file)

                netloc = parse.quote(username, safe='') + ':' + parse.quote(password,
                                                                            safe='') + '@' + parsedInputURL.hostname
                if parsedInputURL.port is not None:
                    netloc += ':' + str(parsedInputURL.port)

                # Now the credentials are properly set up
                remote_file = parse.urlunparse((parsedInputURL.scheme, netloc, parsedInputURL.path,
                                                parsedInputURL.params, parsedInputURL.query, parsedInputURL.fragment))
        with request.urlopen(remote_file) as url_response, open(cachedFilename, 'wb') as download_file:
            shutil.copyfileobj(url_response, download_file)
    except Exception as e:
        raise WFException("Cannot download content from {} to {}: {}".format(remote_file, cachedFilename, e))


class WF:
    """
    Workflow class
    """

    DEFAULT_RO_EXTENSION = ".crate.zip"
    DEFAULT_TRS_ENDPOINT = "https://dev.workflowhub.eu/ga4gh/trs/v2/tools/"  # root of GA4GH TRS API
    DEFAULT_GIT_CMD = 'git'
    WORKFLOW_ENGINES = [
        {
            'engine': 'nextflow',
            'trs_descriptor': 'NFL',
            'rocrate_programming_language': '#nextflow',
        },
        {
            'engine': 'cwl',
            'trs_descriptor': 'CWL',
            'rocrate_programming_language': '#cwl',
        },
    ]

    RECOGNIZED_TRS_DESCRIPTORS = dict(map(lambda t: (t['trs_descriptor'], t), WORKFLOW_ENGINES))
    RECOGNIZED_ROCRATE_PROG_LANG = dict(map(lambda t: (t['rocrate_programming_language'], t), WORKFLOW_ENGINES))

    DEFAULT_SCHEME_HANDLERS = {
        'http': fetchClassicURL,
        'https': fetchClassicURL,
        'ftp': fetchClassicURL,
    }

    @classmethod
    def fromDescription(cls, workflow_config, local_config, creds_config=None):
        """

        :param workflow_config: The configuration describing both the workflow
        and the inputs to use when it is being instantiated.
        :param local_config: Relevant local configuration, like the cache directory.
        :param creds_config:
        :type workflow_config: dict
        :type local_config: dict
        :return: workflow configuration
        """
        if creds_config is None:
            creds_config = {}
        return cls(
            workflow_config['workflow_id'],
            workflow_config['version'],
            descriptor_type=workflow_config.get('workflow_type'),
            trs_endpoint=workflow_config.get('trs_endpoint', cls.DEFAULT_TRS_ENDPOINT),
            params=workflow_config.get('params', {}),
            local_config=local_config,
            creds_config=creds_config
        )

    def __init__(self, workflow_id, version_id, descriptor_type=None, trs_endpoint=DEFAULT_TRS_ENDPOINT, params=None,
                 local_config=None, creds_config=None):
        """
        Init function

        :param workflow_id: A unique identifier of the workflow. Although it is an integer in WorkflowHub,
        we cannot assume it is so in all the GA4GH TRS implementations which are exposing workflows.
        :param version_id: An identifier of the workflow version. Although it is an integer in
        WorkflowHub, we cannot assume the format of the version id, as it could follow semantic
        versioning, providing an UUID, etc.
        :param descriptor_type: The type of descriptor that represents this version of the workflow
        (e.g. CWL, WDL, NFL, or GALAXY). It is optional, so it is guessed from the calls to the API.
        :param trs_endpoint: The TRS endpoint used to find the workflow.
        :param params: Optional params for the workflow execution.
        :param local_config: Local setup configuration, telling where caching directories live
        :param creds_config: Dictionary with the different credential contexts (to be implemented)
        :type workflow_id: str
        :type version_id: str
        :type descriptor_type: str
        :type trs_endpoint: str
        :type params: dict
        :type local_config: dict
        :type creds_config: dict
        """
        if not isinstance(local_config, dict):
            local_config = {}

        if not isinstance(creds_config, dict):
            creds_config = {}

        if not isinstance(params, dict):
            params = {}

        self.id = str(workflow_id)
        self.version_id = str(version_id)
        self.descriptor_type = descriptor_type
        self.params = params
        self.local_config = local_config
        self.creds_config = creds_config

        # The endpoint should always end with a slash
        if isinstance(trs_endpoint, str) and trs_endpoint[-1] != '/':
            trs_endpoint += '/'

        self.trs_endpoint = trs_endpoint

        # This directory will be used to cache repositories
        cacheDir = local_config.get('cacheDir')
        if cacheDir:
            os.makedirs(cacheDir, exist_ok=True)
        else:
            cacheDir = tempfile.mkdtemp(prefix='wfexs', suffix='backend')
            # Assuring this temporal directory is removed at the end
            atexit.register(shutil.rmtree, cacheDir)

        self.git_cmd = local_config.get('gitCommand', self.DEFAULT_GIT_CMD)

        self.cacheDir = cacheDir
        self.cacheWorkflowDir = os.path.join(cacheDir, 'wf-cache')
        os.makedirs(self.cacheWorkflowDir, exist_ok=True)
        self.cacheROCrateDir = os.path.join(cacheDir, 'ro-crate-cache')
        os.makedirs(self.cacheROCrateDir, exist_ok=True)
        self.cacheWorkflowInputsDir = os.path.join(cacheDir, 'wf-inputs')
        os.makedirs(self.cacheWorkflowInputsDir, exist_ok=True)

        self.schemeHandlers = self.DEFAULT_SCHEME_HANDLERS.copy()

        self.repoURL = None
        self.repoTag = None
        self.repoRelPath = None
        self.repoDir = None
        self.engineDesc = None

        self.materializedParams = None

    def fetchWorkflow(self):
        """
        Fetch the whole workflow description based on the data obtained
        from the TRS where it is being published.
        
        If the workflow id is an URL, it is supposed to be a git repository,
        and the version will represent either the branch, tag or specific commit.
        So, the whole TRS fetching machinery is bypassed.
        """
        parsedRepoURL = parse.urlparse(self.id)

        # It is not an absolute URL, so it is being an identifier in the workflow
        if parsedRepoURL.scheme == '':
            engineDesc, repoURL, repoTag, repoRelPath = self.getWorkflowRepoFromTRS()
        else:
            repoURL = self.id
            repoTag = self.version_id
            repoRelPath = None
            engineDesc = None

        self.repoURL = repoURL
        self.repoTag = repoTag
        # It can be either a relative path to a directory or to a file
        # It could be even empty!
        if repoRelPath == '':
            repoRelPath = None
        self.repoRelPath = repoRelPath

        repoDir, repoEngineDesc = self.doMaterializeRepo(repoURL, repoTag)
        print("materialized workflow repository: {}".format(repoDir))

        if engineDesc is None:
            engineDesc = repoEngineDesc

        self.repoDir = repoDir
        self.engineDesc = engineDesc

    def setupEngine(self):
        pass

    def addSchemeHandler(self, scheme, handler):
        if not isinstance(handler, (
                types.FunctionType, types.LambdaType, types.MethodType, types.BuiltinFunctionType,
                types.BuiltinMethodType)):
            raise WFException('Trying to set for scheme {} a invalid handler'.format(scheme))

        self.schemeHandlers[scheme.lower()] = handler

    def materializeInputs(self):
        theParams = self.fetchInputs(self.params, workflowInputs_destdir=self.cacheWorkflowInputsDir)
        self.materializedParams = theParams

    def fetchInputs(self, params, workflowInputs_destdir=None, prefix=''):
        """
        Fetch the input files for the workflow execution.
        All the inputs must be URLs or CURIEs from identifiers.org.
        """
        theInputs = []

        paramsIter = params.items() if isinstance(params, dict) else enumerate(params)
        for key, inputs in paramsIter:
            # We are here for the 
            linearKey = prefix + key
            if isinstance(inputs, dict):
                inputClass = inputs.get('c-l-a-s-s')
                if inputClass is not None:
                    if inputClass == "File":  # input files
                        remote_files = inputs['url']
                        if not isinstance(remote_files, list):  # more than one input file
                            remote_files = [remote_files]

                        remote_pairs = []
                        for remote_file in remote_files:
                            # We are sending the context name thinking in the future,
                            # as it could contain potential hints for authenticated access
                            contextName = inputs.get('security-context')
                            matContent = self.downloadInputFile(remote_file,
                                                                workflowInputs_destdir=workflowInputs_destdir,
                                                                contextName=contextName)
                            remote_pairs.append(matContent)

                        theInputs.append(MaterializedInput(linearKey, remote_pairs))
                    else:
                        raise WFException('Unrecognized input class "{}"'.format(inputClass))
                else:
                    # possible nested files
                    theInputs.extend(
                        self.fetchInputs(inputs, workflowInputs_destdir=workflowInputs_destdir, prefix=linearKey + '.'))
            else:
                if not isinstance(inputs, list):
                    inputs = [inputs]
                theInputs.append(MaterializedInput(linearKey, inputs))

        return theInputs

    def executeWorkflow(self):
        pass

    def doMaterializeRepo(self, repoURL, repoTag=None):
        """

        :param repoURL:
        :param repoTag:
        :return:
        """
        repo_hashed_id = hashlib.sha1(repoURL.encode('utf-8')).hexdigest()
        repo_hashed_tag_id = hashlib.sha1(b'' if repoTag is None else repoTag.encode('utf-8')).hexdigest()

        # Assure directory exists before next step
        repo_destdir = os.path.join(self.cacheWorkflowDir, repo_hashed_id)
        if not os.path.exists(repo_destdir):
            try:
                os.makedirs(repo_destdir)
            except IOError:
                errstr = "ERROR: Unable to create intermediate directories for repo {}. ".format(repoURL)
                raise WFException(errstr)

        repo_tag_destdir = os.path.join(repo_destdir, repo_hashed_tag_id)
        # We are assuming that, if the directory does exist, it contains the repo
        if not os.path.exists(repo_tag_destdir):
            # Try cloning the repository without initial checkout
            if repoTag is not None:
                gitclone_params = [
                    self.git_cmd, 'clone', '-n', '--recurse-submodules', repoURL, repo_tag_destdir
                ]

                # Now, checkout the specific commit
                gitcheckout_params = [
                    self.git_cmd, 'checkout', repoTag
                ]
            else:
                # We know nothing about the tag, or checkout
                gitclone_params = [
                    self.git_cmd, 'clone', '--recurse-submodules', repoURL, repo_tag_destdir
                ]

                gitcheckout_params = None

            # Last, initialize submodules
            gitsubmodule_params = [
                self.git_cmd, 'submodule', 'update', '--init'
            ]

            with tempfile.NamedTemporaryFile() as git_stdout:
                with tempfile.NamedTemporaryFile() as git_stderr:
                    # First, (bare) clone
                    retval = subprocess.call(gitclone_params, stdout=git_stdout, stderr=git_stderr)
                    # Then, checkout (which can be optional)
                    if retval == 0 and (gitcheckout_params is not None):
                        retval = subprocess.Popen(gitcheckout_params, stdout=git_stdout, stderr=git_stderr,
                                                  cwd=repo_tag_destdir).wait()
                    # Last, submodule preparation
                    if retval == 0:
                        retval = subprocess.Popen(gitsubmodule_params, stdout=git_stdout, stderr=git_stderr,
                                                  cwd=repo_tag_destdir).wait()

                    # Proper error handling
                    if retval != 0:
                        # Reading the output and error for the report
                        with open(git_stdout.name, "r") as c_stF:
                            git_stdout_v = c_stF.read()
                        with open(git_stderr.name, "r") as c_stF:
                            git_stderr_v = c_stF.read()

                        errstr = "ERROR: Unable to pull '{}' (tag '{}'). Retval {}\n======\nSTDOUT\n======\n{}\n======\nSTDERR\n======\n{}".format(
                            repoURL, repoTag, retval, git_stdout_v, git_stderr_v)
                        raise WFException(errstr)

        # TODO: guess engine desc, currently hardcoded
        return repo_tag_destdir, self.WORKFLOW_ENGINES[0]

    def getWorkflowRepoFromTRS(self):
        """

        :return:
        """
        # First, check the tool does exist in the TRS, and the version
        trs_tool_url = parse.urljoin(self.trs_endpoint, parse.quote(self.id, safe=''))

        # The original bytes
        response = b''
        with request.urlopen(trs_tool_url) as req:
            while True:
                try:
                    # Try getting it
                    responsePart = req.read()
                except http.client.IncompleteRead as icread:
                    # Getting at least the partial content
                    response += icread.partial
                    continue
                else:
                    # In this case, saving all
                    response += responsePart
                break

        # If the tool does not exist, an exception will be thrown before
        jd = json.JSONDecoder()
        rawToolDesc = response.decode('utf-8')
        toolDesc = jd.decode(rawToolDesc)

        # If the tool is not a workflow, complain
        if toolDesc.get('toolclass', {}).get('name', '') != 'Workflow':
            raise WFException(
                'Tool {} from {} is not labelled as a workflow. Raw answer:\n{}'.format(self.id, self.trs_endpoint,
                                                                                        rawToolDesc))

        possibleToolVersions = toolDesc.get('versions', [])
        if len(possibleToolVersions) == 0:
            raise WFException(
                'Version {} not found in workflow {} from {} . Raw answer:\n{}'.format(self.version_id, self.id,
                                                                                       self.trs_endpoint, rawToolDesc))

        toolVersion = None
        toolVersionId = self.version_id
        if (toolVersionId is not None) and len(toolVersionId) > 0:
            for possibleToolVersion in possibleToolVersions:
                if isinstance(possibleToolVersion, dict) and str(possibleToolVersion.get('id', '')) == self.version_id:
                    toolVersion = possibleToolVersion
                    break
            else:
                raise WFException(
                    'Version {} not found in workflow {} from {} . Raw answer:\n{}'.format(self.version_id, self.id,
                                                                                           self.trs_endpoint,
                                                                                           rawToolDesc))
        else:
            toolVersionId = ''
            for possibleToolVersion in possibleToolVersions:
                possibleToolVersionId = str(possibleToolVersion.get('id', ''))
                if len(possibleToolVersionId) > 0 and toolVersionId < possibleToolVersionId:
                    toolVersion = possibleToolVersion
                    toolVersionId = possibleToolVersionId

        if toolVersion is None:
            raise WFException(
                'No valid version was found in workflow {} from {} . Raw answer:\n{}'.format(self.id, self.trs_endpoint,
                                                                                             rawToolDesc))

        # The version has been found
        toolDescriptorTypes = toolVersion.get('descriptor_type', [])
        if not isinstance(toolDescriptorTypes, list):
            raise WFException(
                'Version {} of workflow {} from {} has no valid "descriptor_type" (should be a list). Raw answer:\n{}'.format(
                    self.version_id, self.id, self.trs_endpoint, rawToolDesc))

        # Now, realize whether it matches
        chosenDescriptorType = self.descriptor_type
        if chosenDescriptorType is None:
            for candidateDescriptorType in self.RECOGNIZED_TRS_DESCRIPTORS.keys():
                if candidateDescriptorType in toolDescriptorTypes:
                    chosenDescriptorType = candidateDescriptorType
                    break
            else:
                raise WFException(
                    'Version {} of workflow {} from {} has no acknowledged "descriptor_type". Raw answer:\n{}'.format(
                        self.version_id, self.id, self.trs_endpoint, rawToolDesc))
        elif chosenDescriptorType not in toolVersion['descriptor_type']:
            raise WFException(
                'Descriptor type {} not available for version {} of workflow {} from {} . Raw answer:\n{}'.format(
                    self.descriptor_type, self.version_id, self.id, self.trs_endpoint, rawToolDesc))
        elif chosenDescriptorType not in self.RECOGNIZED_TRS_DESCRIPTORS:
            raise WFException(
                'Descriptor type {} is not among the acknowledged ones by this backend. Version {} of workflow {} from {} . Raw answer:\n{}'.format(
                    self.descriptor_type, self.version_id, self.id, self.trs_endpoint, rawToolDesc))

        # And this is the moment where the RO-Crate must be fetched
        roCrateURL = trs_tool_url + '/versions/' + parse.quote(toolVersionId,
                                                               safe='') + '/' + parse.quote(
            chosenDescriptorType, safe='') + '/files?' + parse.urlencode({'format': 'zip'})

        return self.getWorkflowRepoFromROCrate(roCrateURL,
            expectedProgrammingLanguage=self.RECOGNIZED_TRS_DESCRIPTORS[chosenDescriptorType]['rocrate_programming_language'])

    def getWorkflowRepoFromROCrate(self, roCrateURL, expectedProgrammingLanguage=None):
        """

        :param roCrateURL:
        :param expectedProgrammingLanguage:
        :return:
        """
        roCrateFile = self.downloadROcrate(roCrateURL)
        print("downloaded RO-Crate: {}".format(roCrateFile))
        roCrateObj = rocrate.ROCrate(roCrateFile)

        # TODO: get roCrateObj mainEntity programming language
        # print(roCrateObj.root_dataset.as_jsonld())
        mainEntityProgrammingLanguage = roCrateObj.get_entities()[5]['@id']  # ComputerLanguage
        # mainEntityProgrammingLanguage = None
        # for e in roCrateObj.get_entities():
        #     if e['@type'] == "ComputerLanguage":
        #         mainEntityProgrammingLanguage = e['@id']
        #         break

        if mainEntityProgrammingLanguage not in self.RECOGNIZED_ROCRATE_PROG_LANG:
            raise WFException(
                'Found programming language {} in RO-Crate manifest is not among the acknowledged ones'.format(
                    mainEntityProgrammingLanguage))
        elif (expectedProgrammingLanguage is not None) and mainEntityProgrammingLanguage != expectedProgrammingLanguage:
            raise WFException(
                'Expected programming language {} does not match found one {} in RO-Crate manifest'.format(
                    expectedProgrammingLanguage, mainEntityProgrammingLanguage))

        # This workflow URL, in the case of github, can provide the repo,
        # the branch/tag/checkout , and the relative directory in the
        # fetched content (needed by Nextflow)
        wf_url = roCrateObj.root_dataset['isBasedOn']

        repoURL = None
        repoTag = None
        repoRelPath = None
        parsed_wf_url = parse.urlparse(wf_url)
        if parsed_wf_url.netloc == 'github.com':
            wf_path = parsed_wf_url.path.split('/')

            if len(wf_path) >= 3:
                repoGitPath = parsed_wf_url.path.split('/')[:3]
                if not repoGitPath[-1].endswith('.git'):
                    repoGitPath[-1] += '.git'

                # Rebuilding repo git path
                repoURL = parse.urlunparse(
                    (parsed_wf_url.scheme, parsed_wf_url.netloc, '/'.join(repoGitPath), '', '', ''))

                # And now, guessing the tag and the relative path
                if len(wf_path) >= 5 and wf_path[3] == 'blob':
                    repoTag = wf_path[4]

                    if len(wf_path) >= 6:
                        repoRelPath = '/'.join(wf_path[5:])
        else:
            raise WFException('Unable to guess repository from RO-Crate manifest')

        # TODO handling other additional cases

        # It must return four elements:
        return self.RECOGNIZED_ROCRATE_PROG_LANG[mainEntityProgrammingLanguage], repoURL, repoTag, repoRelPath

    def downloadROcrate(self, roCrateURL):
        """
        Download RO-crate from WorkflowHub (https://dev.workflowhub.eu/)
        using GA4GH TRS API and save RO-Crate in path.

        :param roCrateURL: location path to save RO-Crate
        :type roCrateURL: str
        :return:
        """
        crate_hashed_id = hashlib.sha1(roCrateURL.encode('utf-8')).hexdigest()
        cachedFilename = os.path.join(self.cacheROCrateDir, crate_hashed_id + self.DEFAULT_RO_EXTENSION)
        if not os.path.exists(cachedFilename):
            try:
                with request.urlopen(roCrateURL) as url_response, open(cachedFilename, "wb") as download_file:
                    shutil.copyfileobj(url_response, download_file)
            except Exception as e:
                raise WFException("Cannot download RO-Crate, {}".format(e))

        return cachedFilename

    def downloadInputFile(self, remote_file, workflowInputs_destdir=None, contextName=None) -> MaterializedContent:
        """
        Download remote file.

        :param remote_file: URL or CURIE to download remote file
        :param contextName:
        :param workflowInputs_destdir:
        :type remote_file: str
        """
        parsedInputURL = parse.urlparse(remote_file)

        if not all([parsedInputURL.scheme, parsedInputURL.netloc, parsedInputURL.path]):
            raise RuntimeError("Input is not a valid remote URL or CURIE source")

        else:
            input_file = hashlib.sha1(remote_file.encode('utf-8')).hexdigest()

            prettyFilename = parsedInputURL.path.split('/')[-1]

            # Assure workflow inputs directory exists before the next step
            if workflowInputs_destdir is None:
                workflowInputs_destdir = self.cacheWorkflowInputsDir

            if not os.path.exists(workflowInputs_destdir):
                try:
                    os.makedirs(workflowInputs_destdir)
                except IOError:
                    errstr = "ERROR: Unable to create directory for workflow inputs {}.".format(workflowInputs_destdir)
                    raise WFException(errstr)

            cachedFilename = os.path.join(self.cacheWorkflowInputsDir, input_file)
            print("downloading workflow input: {} => {}".format(remote_file, cachedFilename))
            if not os.path.exists(cachedFilename):
                theScheme = parsedInputURL.scheme.lower()
                schemeHandler = self.schemeHandlers.get(theScheme)

                if schemeHandler is None:
                    raise WFException('No {} scheme handler for {}'.format(theScheme, remote_file))

                # Security context is obtained here
                secContext = None
                if contextName is not None:
                    secContext = self.creds_config.get(contextName)
                    if secContext is None:
                        raise WFException(
                            'No security context {} is available, needed by {}'.format(contextName, remote_file))

                # Content is fetched here
                schemeHandler(remote_file, cachedFilename, secContext=secContext)

            return MaterializedContent(cachedFilename, remote_file, prettyFilename)