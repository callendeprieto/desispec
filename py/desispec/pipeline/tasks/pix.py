#
# See top-level LICENSE.rst file for Copyright information
#
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function

import os
import re

from collections import OrderedDict

from ..defs import (task_name_sep, task_state_to_int, task_int_to_state)

from ...util import option_list

from ...io import findfile

from .base import BaseTask


# NOTE: only one class in this file should have a name that starts with "Task".

class TaskPix(BaseTask):
    """Class containing the properties of one preprocessed pixel file.
    """
    def __init__(self):
        # do that first
        super(TaskPix, self).__init__()
        # then put int the specifics of this class
        # _cols must have a state
        self._type = "pix"
        self._cols = [
            "night",
            "band",
            "spec",
            "expid",
            "flavor",
            "state"
        ]
        self._coltypes = [
            "integer",
            "text",
            "integer",
            "integer",
            "text",
            "integer"
        ]
        # _name_fields must also be in _cols
        self._name_fields  = ["night","band","spec","expid"]
        self._name_formats = ["08d","s","d","08d"]


    def _paths(self, name):
        """See BaseTask.paths.
        """
        props = self.name_split(name)
        camera = "{}{}".format(props["band"], props["spec"])
        return [ findfile("pix", night=props["night"], expid=props["expid"],
            camera=camera, groupname=None, nside=None, band=props["band"],
            spectrograph=props["spec"]) ]


    def _deps(self, name, db, inputs):
        """See BaseTask.deps.
        """
        from .base import task_classes
        props = self.name_split(name)
        fmap  = task_classes["fibermap"].name_join(props)
        rdata = task_classes["rawdata"].name_join(props)

        deptasks = {
             "fibermap" : fmap,
             "rawdata" : rdata
        }
        return deptasks


    def _run_max_procs(self, procs_per_node):
        """See BaseTask.run_max_procs.
        """
        return 1


    def _run_time(self, name, procs_per_node, db=None):
        """See BaseTask.run_time.
        """
        return 2


    def _run_defaults(self):
        """See BaseTask.run_defaults.
        """
        return dict()


    def _option_list(self, name, opts):
        """Build the full list of options.

        This includes appending the filenames and incorporating runtime
        options.
        """
        from .base import task_classes, task_type

        dp = self.deps(name)

        options = OrderedDict()
        options.update(opts)

        props = self.name_split(name)
        options["infile"] = dp["rawdata"]
        options["cameras"] = "{}{}".format(props["band"],props["spec"])

        outfile = self.paths(name)[0]
        options["pixfile"] = outfile

        return option_list(options)


    def _run_cli(self, name, opts, procs):
        """See BaseTask.run_cli.
        """
        entry = "desi_preproc"
        optlist = self._option_list(name, opts)
        com = "{} {}".format(entry, " ".join(optlist))
        return com


    def _run(self, name, opts, comm):
        """See BaseTask.run.
        """
        from ...scripts import preproc
        optlist = self._option_list(name, opts)
        args = preproc.parse(optarray)
        preproc.main(args)
        return
