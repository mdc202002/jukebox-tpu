"""Provides an API for writing protocol buffers to event files to be
consumed by TensorBoard for visualization."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os
import six
import time
import logging

from .embedding import make_mat, make_sprite, make_tsv, append_pbtxt
from .event_file_writer import EventFileWriter
from .onnx_graph import load_onnx_graph
from .pytorch_graph import graph
from .proto import event_pb2
from .proto import summary_pb2
from .proto.event_pb2 import SessionLog, Event
from .utils import figure_to_image
from .summary import (
    scalar, histogram, histogram_raw, image, audio, text,
    pr_curve, pr_curve_raw, video, custom_scalars, image_boxes, mesh, hparams
)


class DummyFileWriter(object):
    """A fake file writer that writes nothing to the disk.
    """
    def __init__(self, logdir):
        self._logdir = logdir

    def get_logdir(self):
        """Returns the directory where event file will be written."""
        return self._logdir

    def add_event(self, event, step=None, walltime=None):
        return

    def add_summary(self, summary, global_step=None, walltime=None):
        return

    def add_graph(self, graph_profile, walltime=None):
        return

    def add_onnx_graph(self, graph, walltime=None):
        return

    def flush(self):
        return

    def close(self):
        return

    def reopen(self):
        return


class FileWriter(object):
    """Writes protocol buffers to event files to be consumed by TensorBoard.

    The `FileWriter` class provides a mechanism to create an event file in a
    given directory and add summaries and events to it. The class updates the
    file contents asynchronously. This allows a training program to call methods
    to add data to the file directly from the training loop, without slowing down
    training.
    """

    def __init__(self, logdir, max_queue=10, flush_secs=120, filename_suffix=''):
        """Creates a `FileWriter` and an event file.
        On construction the writer creates a new event file in `logdir`.
        The other arguments to the constructor control the asynchronous writes to
        the event file.

        Args:
          logdir: A string. Directory where event file will be written.
          max_queue: Integer. Size of the queue for pending events and
            summaries before one of the 'add' calls forces a flush to disk.
            Default is ten items.
          flush_secs: Number. How often, in seconds, to flush the
            pending events and summaries to disk. Default is every two minutes.
          filename_suffix: A string. Suffix added to all event filenames
            in the logdir directory. More details on filename construction in
            tensorboard.summary.writer.event_file_writer.EventFileWriter.
        """
        # Sometimes PosixPath is passed in and we need to coerce it to
        # a string in all cases
        # TODO: See if we can remove this in the future if we are
        # actually the ones passing in a PosixPath
        logdir = str(logdir)
        self.event_writer = EventFileWriter(
            logdir, max_queue, flush_secs, filename_suffix)

    def get_logdir(self):
        """Returns the directory where event file will be written."""
        return self.event_writer.get_logdir()

    def add_event(self, event, step=None, walltime=None):
        """Adds an event to the event file.
        Args:
          event: An `Event` protocol buffer.
          step: Number. Optional global step value for training process
            to record with the event.
          walltime: float. Optional walltime to override the default (current)
            walltime (from time.time())
        """
        event.wall_time = time.time() if walltime is None else walltime
        if step is not None:
            # Make sure step is converted from numpy or other formats
            # since protobuf might not convert depending on version
            event.step = int(step)
        self.event_writer.add_event(event)

    def add_summary(self, summary, global_step=None, walltime=None):
        """Adds a `Summary` protocol buffer to the event file.
        This method wraps the provided summary in an `Event` protocol buffer
        and adds it to the event file.

        Args:
          summary: A `Summary` protocol buffer.
          global_step: Number. Optional global step value for training process
            to record with the summary.
          walltime: float. Optional walltime to override the default (current)
            walltime (from time.time())
        """
        event = event_pb2.Event(summary=summary)
        self.add_event(event, global_step, walltime)

    def add_graph(self, graph_profile, walltime=None):
        """Adds a `Graph` and step stats protocol buffer to the event file.

        Args:
          graph_profile: A `Graph` and step stats protocol buffer.
          walltime: float. Optional walltime to override the default (current)
            walltime (from time.time()) seconds after epoch
        """
        graph = graph_profile[0]
        stepstats = graph_profile[1]
        event = event_pb2.Event(graph_def=graph.SerializeToString())
        self.add_event(event, None, walltime)

        trm = event_pb2.TaggedRunMetadata(
            tag='step1', run_metadata=stepstats.SerializeToString())
        event = event_pb2.Event(tagged_run_metadata=trm)
        self.add_event(event, None, walltime)

    def add_onnx_graph(self, graph, walltime=None):
        """Adds a `Graph` protocol buffer to the event file.

        Args:
          graph: A `Graph` protocol buffer.
          walltime: float. Optional walltime to override the default (current)
            _get_file_writerfrom time.time())
        """
        event = event_pb2.Event(graph_def=graph.SerializeToString())
        self.add_event(event, None, walltime)

    def flush(self):
        """Flushes the event file to disk.
        Call this method to make sure that all pending events have been written to
        disk.
        """
        self.event_writer.flush()

    def close(self):
        """Flushes the event file to disk and close the file.
        Call this method when you do not need the summary writer anymore.
        """
        self.event_writer.close()

    def reopen(self):
        """Reopens the EventFileWriter.
        Can be called after `close()` to add more events in the same directory.
        The events will go into a new events file.
        Does nothing if the EventFileWriter was not closed.
        """
        self.event_writer.reopen()


class SummaryWriter(object):
    """Writes entries directly to event files in the logdir to be
    consumed by TensorBoard.

    The `SummaryWriter` class provides a high-level API to create an event file
    in a given directory and add summaries and events to it. The class updates the
    file contents asynchronously. This allows a training program to call methods
    to add data to the file directly from the training loop, without slowing down
    training.
    """

    def __init__(self, logdir=None, comment='', purge_step=None, max_queue=10,
                 flush_secs=120, filename_suffix='', write_to_disk=True, log_dir=None, **kwargs):
        """Creates a `SummaryWriter` that will write out events and summaries
        to the event file.

        Args:
            logdir (string): Save directory location. Default is
              runs/**CURRENT_DATETIME_HOSTNAME**, which changes after each run.
              Use hierarchical folder structure to compare
              between runs easily. e.g. pass in 'runs/exp1', 'runs/exp2', etc.
              for each new experiment to compare across them.
            comment (string): Comment logdir suffix appended to the default
              ``logdir``. If ``logdir`` is assigned, this argument has no effect.
            purge_step (int):
              When logging crashes at step :math:`T+X` and restarts at step :math:`T`,
              any events whose global_step larger or equal to :math:`T` will be
              purged and hidden from TensorBoard.
              Note that crashed and resumed experiments should have the same ``logdir``.
            max_queue (int): Size of the queue for pending events and
              summaries before one of the 'add' calls forces a flush to disk.
              Default is ten items.
            flush_secs (int): How often, in seconds, to flush the
              pending events and summaries to disk. Default is every two minutes.
            filename_suffix (string): Suffix added to all event filenames in
              the logdir directory. More details on filename construction in
              tensorboard.summary.writer.event_file_writer.EventFileWriter.
            write_to_disk (boolean):
              If pass `False`, SummaryWriter will not write to disk.

        Examples::

            from tensorboardX import SummaryWriter

            # create a summary writer with automatically generated folder name.
            writer = SummaryWriter()
            # folder location: runs/May04_22-14-54_s-MacBook-Pro.local/

            # create a summary writer using the specified folder name.
            writer = SummaryWriter("my_experiment")
            # folder location: my_experiment

            # create a summary writer with comment appended.
            writer = SummaryWriter(comment="LR_0.1_BATCH_16")
            # folder location: runs/May04_22-14-54_s-MacBook-Pro.localLR_0.1_BATCH_16/

        """
        if log_dir is not None and logdir is None:
            logdir = log_dir
        if not logdir:
            import socket
            from datetime import datetime
            current_time = datetime.now().strftime('%b%d_%H-%M-%S')
            logdir = os.path.join(
                'runs', current_time + '_' + socket.gethostname() + comment)
        self.logdir = logdir
        self.purge_step = purge_step
        self._max_queue = max_queue
        self._flush_secs = flush_secs
        self._filename_suffix = filename_suffix
        self._write_to_disk = write_to_disk
        self.kwargs = kwargs

        # Initialize the file writers, but they can be cleared out on close
        # and recreated later as needed.
        self.file_writer = self.all_writers = None
        self._get_file_writer()

        # Create default bins for histograms, see generate_testdata.py in tensorflow/tensorboard
        v = 1E-12
        buckets = []
        neg_buckets = []
        while v < 1E20:
            buckets.append(v)
            neg_buckets.append(-v)
            v *= 1.1
        self.default_bins = neg_buckets[::-1] + [0] + buckets

        self.scalar_dict = {}

    def __append_to_scalar_dict(self, tag, scalar_value, global_step,
                                timestamp):
        """This adds an entry to the self.scalar_dict datastructure with format
        {writer_id : [[timestamp, step, value], ...], ...}.
        """
        from .x2num import make_np
        if tag not in self.scalar_dict.keys():
            self.scalar_dict[tag] = []
        self.scalar_dict[tag].append(
            [timestamp, global_step, float(make_np(scalar_value))])

    def _check_caffe2_blob(self, item):
        """
        Caffe2 users have the option of passing a string representing the name of
        a blob in the workspace instead of passing the actual Tensor/array containing
        the numeric values. Thus, we need to check if we received a string as input
        instead of an actual Tensor/array, and if so, we need to fetch the Blob
        from the workspace corresponding to that name. Fetching can be done with the
        following:

        from caffe2.python import workspace (if not already imported)
        workspace.FetchBlob(blob_name)
        workspace.FetchBlobs([blob_name1, blob_name2, ...])
        """
        return isinstance(item, six.string_types)

    def _get_file_writer(self):
        """Returns the default FileWriter instance. Recreates it if closed."""
        if not self._write_to_disk:
            self.file_writer = DummyFileWriter(logdir=self.logdir)
            self.all_writers = {self.file_writer.get_logdir(): self.file_writer}
            return self.file_writer

        if self.all_writers is None or self.file_writer is None:
            if 'purge_step' in self.kwargs.keys():
                most_recent_step = self.kwargs.pop('purge_step')
                self.file_writer = FileWriter(logdir=self.logdir,
                                              max_queue=self._max_queue,
                                              flush_secs=self._flush_secs,
                                              filename_suffix=self._filename_suffix,
                                              **self.kwargs)
                self.file_writer.add_event(
                    Event(step=most_recent_step, file_version='brain.Event:2'))
                self.file_writer.add_event(
                    Event(step=most_recent_step, session_log=SessionLog(status=SessionLog.START)))
            else:
                self.file_writer = FileWriter(logdir=self.logdir,
                                              max_queue=self._max_queue,
                                              flush_secs=self._flush_secs,
                                              filename_suffix=self._filename_suffix,
                                              **self.kwargs)
            self.all_writers = {self.file_writer.get_logdir(): self.file_writer}
        return self.file_writer

    def add_hparams(self, hparam_dict=None, metric_dict=None):
        """Add a set of hyperparameters to be compared in tensorboard.

        Args:
            hparam_dict (dictionary): Each key-value pair in the dictionary is the
              name of the hyper parameter and it's corresponding value.
            metric_dict (dictionary): Each key-value pair in the dictionary is the
              name of the metric and it's corresponding value. Note that the key used
              here should be unique in the tensorboard record. Otherwise the value
              you added by `add_scalar` will be displayed in hparam plugin. In most
              cases, this is unwanted.

        Examples::

            from tensorboardX import SummaryWriter
            with SummaryWriter() as w:
                for i in range(5):
                    w.add_hparams({'lr': 0.1*i, 'bsize': i},
                                  {'hparam/accuracy': 10*i, 'hparam/loss': 10*i})

        Expected result:

        .. image:: _static/img/tensorboard/add_hparam.png
           :scale: 50 %
        """
        if type(hparam_dict) is not dict or type(metric_dict) is not dict:
            raise TypeError('hparam_dict and metric_dict should be dictionary.')
        exp, ssi, sei = hparams(hparam_dict, metric_dict)

        with SummaryWriter(logdir=os.path.join(self.file_writer.get_logdir(), str(time.time()))) as w_hp:
            w_hp.file_writer.add_summary(exp)
            w_hp.file_writer.add_summary(ssi)
            w_hp.file_writer.add_summary(sei)
            for k, v in metric_dict.items():
                w_hp.add_scalar(k, v)

    def add_scalar(self, tag, scalar_value, global_step=None, walltime=None):
        """Add scalar data to summary.

        Args:
            tag (string): Data identifier
            scalar_value (float or string/blobname): Value to save
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event

        Examples::

            from tensorboardX import SummaryWriter
            writer = SummaryWriter()
            x = range(100)
            for i in x:
                writer.add_scalar('y=2x', i * 2, i)
            writer.close()

        Expected result:

        .. image:: _static/img/tensorboard/add_scalar.png
           :scale: 50 %

        """
        if self._check_caffe2_blob(scalar_value):
            scalar_value = workspace.FetchBlob(scalar_value)
        self._get_file_writer().add_summary(
            scalar(tag, scalar_value), global_step, walltime)

    def add_scalars(self, main_tag, tag_scalar_dict, global_step=None, walltime=None):
        """Adds many scalar data to summary.

        Note that this function also keeps logged scalars in memory. In extreme case it explodes your RAM.

        Args:
            main_tag (string): The parent name for the tags
            tag_scalar_dict (dict): Key-value pair storing the tag and corresponding values
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event

        Examples::

            from tensorboardX import SummaryWriter
            writer = SummaryWriter()
            r = 5
            for i in range(100):
                writer.add_scalars('run_14h', {'xsinx':i*np.sin(i/r),
                                                'xcosx':i*np.cos(i/r),
                                                'tanx': np.tan(i/r)}, i)
            writer.close()
            # This call adds three values to the same scalar plot with the tag
            # 'run_14h' in TensorBoard's scalar section.

        Expected result:

        .. image:: _static/img/tensorboard/add_scalars.png
           :scale: 50 %

        """
        walltime = time.time() if walltime is None else walltime
        fw_logdir = self._get_file_writer().get_logdir()
        for tag, scalar_value in tag_scalar_dict.items():
            fw_tag = fw_logdir + "/" + main_tag + "/" + tag
            if fw_tag in self.all_writers.keys():
                fw = self.all_writers[fw_tag]
            else:
                fw = FileWriter(logdir=fw_tag)
                self.all_writers[fw_tag] = fw
            if self._check_caffe2_blob(scalar_value):
                scalar_value = workspace.FetchBlob(scalar_value)
            fw.add_summary(scalar(main_tag, scalar_value),
                           global_step, walltime)
            self.__append_to_scalar_dict(
                fw_tag, scalar_value, global_step, walltime)

    def export_scalars_to_json(self, path):
        """Exports to the given path an ASCII file containing all the scalars written
        so far by this instance, with the following format:
        {writer_id : [[timestamp, step, value], ...], ...}

        The scalars saved by ``add_scalars()`` will be flushed after export.
        """
        with open(path, "w") as f:
            json.dump(self.scalar_dict, f)
        self.scalar_dict = {}

    def add_histogram(self, tag, values, global_step=None, bins='tensorflow', walltime=None, max_bins=None):
        """Add histogram to summary.

        Args:
            tag (string): Data identifier
            values (torch.Tensor, numpy.array, or string/blobname): Values to build histogram
            global_step (int): Global step value to record
            bins (string): One of {'tensorflow','auto', 'fd', ...}. This determines how the bins are made. You can find
              other options in: https://docs.scipy.org/doc/numpy/reference/generated/numpy.histogram.html
            walltime (float): Optional override default walltime (time.time()) of event

        Examples::

            from tensorboardX import SummaryWriter
            import numpy as np
            writer = SummaryWriter()
            for i in range(10):
                x = np.random.random(1000)
                writer.add_histogram('distribution centers', x + i, i)
            writer.close()

        Expected result:

        .. image:: _static/img/tensorboard/add_histogram.png
           :scale: 50 %

        """
        if self._check_caffe2_blob(values):
            values = workspace.FetchBlob(values)
        if isinstance(bins, six.string_types) and bins == 'tensorflow':
            bins = self.default_bins
        self._get_file_writer().add_summary(
            histogram(tag, values, bins, max_bins=max_bins), global_step, walltime)

    def add_histogram_raw(self, tag, min, max, num, sum, sum_squares,
                          bucket_limits, bucket_counts, global_step=None,
                          walltime=None):
        """Adds histogram with raw data.

        Args:
            tag (string): Data identifier
            min (float or int): Min value
            max (float or int): Max value
            num (int): Number of values
            sum (float or int): Sum of all values
            sum_squares (float or int): Sum of squares for all values
            bucket_limits (torch.Tensor, numpy.array): Upper value per
              bucket, note that the bucket_limits returned from `np.histogram`
              has one more element. See the comment in the following example.
            bucket_counts (torch.Tensor, numpy.array): Number of values per bucket
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event

        Examples::

            import numpy as np
            dummy_data = []
            for idx, value in enumerate(range(30)):
                dummy_data += [idx + 0.001] * value
            values = np.array(dummy_data).astype(float).reshape(-1).contiguous()
            counts, limits = np.histogram(values)
            sum_sq = values.dot(values)
            with SummaryWriter() as summary_writer:
                summary_writer.add_histogram_raw(
                        tag='hist_dummy_data',
                        min=values.min(),
                        max=values.max(),
                        num=len(values),
                        sum=values.sum(),
                        sum_squares=sum_sq,
                        bucket_limits=limits[1:].tolist(),  # <- note here.
                        bucket_counts=counts.tolist(),
                        global_step=0)

        """
        if len(bucket_limits) != len(bucket_counts):
            raise ValueError('len(bucket_limits) != len(bucket_counts), see the document.')
        self._get_file_writer().add_summary(
            histogram_raw(tag,
                          min,
                          max,
                          num,
                          sum,
                          sum_squares,
                          bucket_limits,
                          bucket_counts),
            global_step,
            walltime)

    def add_image(self, tag, img_tensor, global_step=None, walltime=None, dataformats='CHW'):
        """Add image data to summary.

        Note that this requires the ``pillow`` package.

        Args:
            tag (string): Data identifier
            img_tensor (torch.Tensor, numpy.array, or string/blobname): An `uint8` or `float`
                Tensor of shape `[channel, height, width]` where `channel` is 1, 3, or 4.
                The elements in img_tensor can either have values in [0, 1] (float32) or [0, 255] (uint8).
                Users are responsible to scale the data in the correct range/type.
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event.
            dataformats (string): This parameter specifies the meaning of each dimension of the input tensor.
        Shape:
            img_tensor: Default is :math:`(3, H, W)`. You can use ``torchvision.utils.make_grid()`` to
            convert a batch of tensor into 3xHxW format or use ``add_images()`` and let us do the job.
            Tensor with :math:`(1, H, W)`, :math:`(H, W)`, :math:`(H, W, 3)` is also suitible as long as
            corresponding ``dataformats`` argument is passed. e.g. CHW, HWC, HW.

        Examples::

            from tensorboardX import SummaryWriter
            import numpy as np
            img = np.zeros((3, 100, 100))
            img[0] = np.arange(0, 10000).reshape(100, 100).contiguous() / 10000
            img[1] = 1 - np.arange(0, 10000).reshape(100, 100).contiguous() / 10000

            img_HWC = np.zeros((100, 100, 3))
            img_HWC[:, :, 0] = np.arange(0, 10000).reshape(100, 100).contiguous() / 10000
            img_HWC[:, :, 1] = 1 - np.arange(0, 10000).reshape(100, 100).contiguous() / 10000

            writer = SummaryWriter()
            writer.add_image('my_image', img, 0)

            # If you have non-default dimension setting, set the dataformats argument.
            writer.add_image('my_image_HWC', img_HWC, 0, dataformats='HWC')
            writer.close()

        Expected result:

        .. image:: _static/img/tensorboard/add_image.png
           :scale: 50 %

        """
        if self._check_caffe2_blob(img_tensor):
            img_tensor = workspace.FetchBlob(img_tensor)
        self._get_file_writer().add_summary(
            image(tag, img_tensor, dataformats=dataformats), global_step, walltime)

    def add_images(self, tag, img_tensor, global_step=None, walltime=None, dataformats='NCHW'):
        """Add batched (4D) image data to summary.
        Besides passing 4D (NCHW) tensor, you can also pass a list of tensors of the same size.
        In this case, the ``dataformats`` should be `CHW` or `HWC`.
        Note that this requires the ``pillow`` package.

        Args:
            tag (string): Data identifier
            img_tensor (torch.Tensor, numpy.array, or string/blobname): Image data
                The elements in img_tensor can either have values in [0, 1] (float32) or [0, 255] (uint8).
                Users are responsible to scale the data in the correct range/type.
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event
        Shape:
            img_tensor: Default is :math:`(N, 3, H, W)`. If ``dataformats`` is specified, other shape will be
            accepted. e.g. NCHW or NHWC.

        Examples::

            from tensorboardX import SummaryWriter
            import numpy as np

            img_batch = np.zeros((16, 3, 100, 100))
            for i in range(16):
                img_batch[i, 0] = np.arange(0, 10000).reshape(100, 100).contiguous() / 10000 / 16 * i
                img_batch[i, 1] = (1 - np.arange(0, 10000).reshape(100, 100).contiguous() / 10000) / 16 * i

            writer = SummaryWriter()
            writer.add_images('my_image_batch', img_batch, 0)
            writer.close()

        Expected result:

        .. image:: _static/img/tensorboard/add_images.png
           :scale: 30 %

        """
        if self._check_caffe2_blob(img_tensor):
            img_tensor = workspace.FetchBlob(img_tensor)
        if isinstance(img_tensor, list):  # a list of tensors in CHW or HWC
            if dataformats.upper() != 'CHW' and dataformats.upper() != 'HWC':
                print('A list of image is passed, but the dataformat is neither CHW nor HWC.')
                print('Nothing is written.')
                return
            import torch
            try:
                img_tensor = torch.stack(img_tensor, 0)
            except TypeError as e:
                import numpy as np
                img_tensor = np.stack(img_tensor, 0)

            dataformats = 'N' + dataformats

        self._get_file_writer().add_summary(
            image(tag, img_tensor, dataformats=dataformats), global_step, walltime)

    def add_image_with_boxes(self, tag, img_tensor, box_tensor, global_step=None,
                             walltime=None, dataformats='CHW', labels=None, **kwargs):
        """Add image and draw bounding boxes on the image.

        Args:
            tag (string): Data identifier
            img_tensor (torch.Tensor, numpy.array, or string/blobname): Image data
            box_tensor (torch.Tensor, numpy.array, or string/blobname): Box data (for detected objects)
              box should be represented as [x1, y1, x2, y2].
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event
            labels (list of string): The strings to be show on each bounding box.
        Shape:
            img_tensor: Default is :math:`(3, H, W)`. It can be specified with ``dataformat`` agrument.
            e.g. CHW or HWC

            box_tensor: (torch.Tensor, numpy.array, or string/blobname): NX4,  where N is the number of
            boxes and each 4 elememts in a row represents (xmin, ymin, xmax, ymax).
        """
        if self._check_caffe2_blob(img_tensor):
            img_tensor = workspace.FetchBlob(img_tensor)
        if self._check_caffe2_blob(box_tensor):
            box_tensor = workspace.FetchBlob(box_tensor)
        if labels is not None:
            if isinstance(labels, str):
                labels = [labels]
            if len(labels) != box_tensor.shape[0]:
                logging.warning('Number of labels do not equal to number of box, skip the labels.')
                labels = None
        self._get_file_writer().add_summary(image_boxes(
            tag, img_tensor, box_tensor, dataformats=dataformats, labels=labels, **kwargs), global_step, walltime)

    def add_figure(self, tag, figure, global_step=None, close=True, walltime=None):
        """Render matplotlib figure into an image and add it to summary.

        Note that this requires the ``matplotlib`` package.

        Args:
            tag (string): Data identifier
            figure (matplotlib.pyplot.figure) or list of figures: Figure or a list of figures
            global_step (int): Global step value to record
            close (bool): Flag to automatically close the figure
            walltime (float): Optional override default walltime (time.time()) of event
        """
        if isinstance(figure, list):
            self.add_image(tag, figure_to_image(figure, close), global_step, walltime, dataformats='NCHW')
        else:
            self.add_image(tag, figure_to_image(figure, close), global_step, walltime, dataformats='CHW')

    def add_video(self, tag, vid_tensor, global_step=None, fps=4, walltime=None):
        """Add video data to summary.

        Note that this requires the ``moviepy`` package.

        Args:
            tag (string): Data identifier
            vid_tensor (torch.Tensor): Video data
            global_step (int): Global step value to record
            fps (float or int): Frames per second
            walltime (float): Optional override default walltime (time.time()) of event
        Shape:
            vid_tensor: :math:`(N, T, C, H, W)`. The values should lie in [0, 255] for type
              `uint8` or [0, 1] for type `float`.
        """
        self._get_file_writer().add_summary(
            video(tag, vid_tensor, fps), global_step, walltime)

    def add_audio(self, tag, snd_tensor, global_step=None, sample_rate=44100, walltime=None):
        """Add audio data to summary.

        Args:
            tag (string): Data identifier
            snd_tensor (torch.Tensor): Sound data
            global_step (int): Global step value to record
            sample_rate (int): sample rate in Hz
            walltime (float): Optional override default walltime (time.time()) of event
        Shape:
            snd_tensor: :math:`(L, c)`. The values should lie between [-1, 1].
        """
        if self._check_caffe2_blob(snd_tensor):
            snd_tensor = workspace.FetchBlob(snd_tensor)
        self._get_file_writer().add_summary(
            audio(tag, snd_tensor, sample_rate=sample_rate), global_step, walltime)

    def add_text(self, tag, text_string, global_step=None, walltime=None):
        """Add text data to summary.

        Args:
            tag (string): Data identifier
            text_string (string): String to save
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time()) of event
        Examples::

            writer.add_text('lstm', 'This is an lstm', 0)
            writer.add_text('rnn', 'This is an rnn', 10)
        """
        self._get_file_writer().add_summary(
            text(tag, text_string), global_step, walltime)

    def add_onnx_graph(self, prototxt):
        self._get_file_writer().add_onnx_graph(load_onnx_graph(prototxt))

    def add_graph(self, model, input_to_model=None, verbose=False, **kwargs):
        # prohibit second call?
        # no, let tensorboard handle it and show its warning message.
        """Add graph data to summary.

        Args:
            model (torch.nn.Module): Model to draw.
            input_to_model (torch.Tensor or list of torch.Tensor): A variable or a tuple of
                variables to be fed.
            verbose (bool): Whether to print graph structure in console.
            omit_useless_nodes (bool): Default to ``true``, which eliminates unused nodes.
            operator_export_type (string): One of: ``"ONNX"``, ``"RAW"``. This determines
                the optimization level of the graph. If error happens during exporting
                the graph, using ``"RAW"`` might help.

        """
        if hasattr(model, 'forward'):
            # A valid PyTorch model should have a 'forward' method
            import torch
            from distutils.version import LooseVersion
            if LooseVersion(torch.__version__) >= LooseVersion("0.3.1"):
                pass
            else:
                if LooseVersion(torch.__version__) >= LooseVersion("0.3.0"):
                    print('You are using PyTorch==0.3.0, use add_onnx_graph()')
                    return
                if not hasattr(torch.autograd.Variable, 'grad_fn'):
                    print('add_graph() only supports PyTorch v0.2.')
                    return
            self._get_file_writer().add_graph(graph(model, input_to_model, verbose, **kwargs))
        else:
            # Caffe2 models do not have the 'forward' method
            from caffe2.proto import caffe2_pb2
            from caffe2.python import core
            from .caffe2_graph import (
                model_to_graph_def, nets_to_graph_def, protos_to_graph_def
            )
            if isinstance(model, list):
                if isinstance(model[0], core.Net):
                    current_graph = nets_to_graph_def(
                        model, **kwargs)
                elif isinstance(model[0], caffe2_pb2.NetDef):
                    current_graph = protos_to_graph_def(
                        model, **kwargs)
            else:
                # Handles cnn.CNNModelHelper, model_helper.ModelHelper
                current_graph = model_to_graph_def(
                    model, **kwargs)
            event = event_pb2.Event(
                graph_def=current_graph.SerializeToString())
            self._get_file_writer().add_event(event)

    @staticmethod
    def _encode(rawstr):
        # I'd use urllib but, I'm unsure about the differences from python3 to python2, etc.
        retval = rawstr
        retval = retval.replace("%", "%%%02x" % (ord("%")))
        retval = retval.replace("/", "%%%02x" % (ord("/")))
        retval = retval.replace("\\", "%%%02x" % (ord("\\")))
        return retval

    def add_embedding(self, mat, metadata=None, label_img=None, global_step=None, tag='default', metadata_header=None):
        """Add embedding projector data to summary.

        Args:
            mat (torch.Tensor or numpy.array): A matrix which each row is the feature vector of the data point
            metadata (list): A list of labels, each element will be convert to string
            label_img (torch.Tensor or numpy.array): Images correspond to each data point. Each image should be square.
            global_step (int): Global step value to record
            tag (string): Name for the embedding
        Shape:
            mat: :math:`(N, D)`, where N is number of data and D is feature dimension

            label_img: :math:`(N, C, H, W)`, where `Height` should be equal to `Width`.

        Examples::

            import keyword
            import torch
            meta = []
            while len(meta)<100:
                meta = meta+keyword.kwlist # get some strings
            meta = meta[:100]

            for i, v in enumerate(meta):
                meta[i] = v+str(i)

            label_img = torch.rand(100, 3, 32, 32)
            for i in range(100):
                label_img[i]*=i/100.0

            writer.add_embedding(torch.randn(100, 5), metadata=meta, label_img=label_img)
            writer.add_embedding(torch.randn(100, 5), label_img=label_img)
            writer.add_embedding(torch.randn(100, 5), metadata=meta)
        """
        from .x2num import make_np
        mat = make_np(mat)
        if global_step is None:
            global_step = 0
            # clear pbtxt?
        # Maybe we should encode the tag so slashes don't trip us up?
        # I don't think this will mess us up, but better safe than sorry.
        subdir = "%s/%s" % (str(global_step).zfill(5), self._encode(tag))
        save_path = os.path.join(self._get_file_writer().get_logdir(), subdir)
        try:
            os.makedirs(save_path)
        except OSError:
            print(
                'warning: Embedding dir exists, did you set global_step for add_embedding()?')
        if metadata is not None:
            assert mat.shape[0] == len(
                metadata), '#labels should equal with #data points'
            make_tsv(metadata, save_path, metadata_header=metadata_header)
        if label_img is not None:
            assert mat.shape[0] == label_img.shape[0], '#images should equal with #data points'
            assert label_img.shape[2] == label_img.shape[3], 'Image should be square, see tensorflow/tensorboard#670'
            make_sprite(label_img, save_path)
        assert mat.ndim == 2, 'mat should be 2D, where mat.size(0) is the number of data points'
        make_mat(mat, save_path)
        # new funcion to append to the config file a new embedding
        append_pbtxt(metadata, label_img,
                     self._get_file_writer().get_logdir(), subdir, global_step, tag)

    def add_pr_curve(self, tag, labels, predictions, global_step=None,
                     num_thresholds=127, weights=None, walltime=None):
        """Adds precision recall curve.
        Plotting a precision-recall curve lets you understand your model's
        performance under different threshold settings. With this function,
        you provide the ground truth labeling (T/F) and prediction confidence
        (usually the output of your model) for each target. The TensorBoard UI
        will let you choose the threshold interactively.

        Args:
            tag (string): Data identifier
            labels (torch.Tensor, numpy.array, or string/blobname):
              Ground truth data. Binary label for each element.
            predictions (torch.Tensor, numpy.array, or string/blobname):
              The probability that an element be classified as true.
              Value should in [0, 1]
            global_step (int): Global step value to record
            num_thresholds (int): Number of thresholds used to draw the curve.
            walltime (float): Optional override default walltime (time.time()) of event

        Examples::

            from tensorboardX import SummaryWriter
            import numpy as np
            labels = np.random.randint(2, size=100)  # binary label
            predictions = np.random.rand(100)
            writer = SummaryWriter()
            writer.add_pr_curve('pr_curve', labels, predictions, 0)
            writer.close()

        """
        from .x2num import make_np
        labels, predictions = make_np(labels), make_np(predictions)
        self._get_file_writer().add_summary(
            pr_curve(tag, labels, predictions, num_thresholds, weights),
            global_step, walltime)

    def add_pr_curve_raw(self, tag, true_positive_counts,
                         false_positive_counts,
                         true_negative_counts,
                         false_negative_counts,
                         precision,
                         recall,
                         global_step=None,
                         num_thresholds=127,
                         weights=None,
                         walltime=None):
        """Adds precision recall curve with raw data.

        Args:
            tag (string): Data identifier
            true_positive_counts (torch.Tensor, numpy.array, or string/blobname): true positive counts
            false_positive_counts (torch.Tensor, numpy.array, or string/blobname): false positive counts
            true_negative_counts (torch.Tensor, numpy.array, or string/blobname): true negative counts
            false_negative_counts (torch.Tensor, numpy.array, or string/blobname): false negative counts
            precision (torch.Tensor, numpy.array, or string/blobname): precision
            recall (torch.Tensor, numpy.array, or string/blobname): recall
            global_step (int): Global step value to record
            num_thresholds (int): Number of thresholds used to draw the curve.
            walltime (float): Optional override default walltime (time.time()) of event
            see: https://github.com/tensorflow/tensorboard/blob/master/tensorboard/plugins/pr_curve/README.md
        """
        self._get_file_writer().add_summary(
            pr_curve_raw(tag,
                         true_positive_counts,
                         false_positive_counts,
                         true_negative_counts,
                         false_negative_counts,
                         precision,
                         recall,
                         num_thresholds,
                         weights),
            global_step,
            walltime)

    def add_custom_scalars_multilinechart(self, tags, category='default', title='untitled'):
        """Shorthand for creating multilinechart. Similar to ``add_custom_scalars()``, but the only necessary argument
        is *tags*.

        Args:
            tags (list): list of tags that have been used in ``add_scalar()``

        Examples::

            writer.add_custom_scalars_multilinechart(['twse/0050', 'twse/2330'])
        """
        layout = {category: {title: ['Multiline', tags]}}
        self._get_file_writer().add_summary(custom_scalars(layout))

    def add_custom_scalars_marginchart(self, tags, category='default', title='untitled'):
        """Shorthand for creating marginchart. Similar to ``add_custom_scalars()``, but the only necessary argument
        is *tags*, which should have exactly 3 elements.

        Args:
            tags (list): list of tags that have been used in ``add_scalar()``

        Examples::

            writer.add_custom_scalars_marginchart(['twse/0050', 'twse/2330', 'twse/2006'])
        """
        assert len(tags) == 3
        layout = {category: {title: ['Margin', tags]}}
        self._get_file_writer().add_summary(custom_scalars(layout))

    def add_custom_scalars(self, layout):
        """Create special chart by collecting charts tags in 'scalars'. Note that this function can only be called once
        for each SummaryWriter() object. Because it only provides metadata to tensorboard, the function can be called
        before or after the training loop. See ``examples/demo_custom_scalars.py`` for more.

        Args:
            layout (dict): {categoryName: *charts*}, where *charts* is also a dictionary
              {chartName: *ListOfProperties*}. The first element in *ListOfProperties* is the chart's type
              (one of **Multiline** or **Margin**) and the second element should be a list containing the tags
              you have used in add_scalar function, which will be collected into the new chart.

        Examples::

            layout = {'Taiwan':{'twse':['Multiline',['twse/0050', 'twse/2330']]},
                         'USA':{ 'dow':['Margin',   ['dow/aaa', 'dow/bbb', 'dow/ccc']],
                              'nasdaq':['Margin',   ['nasdaq/aaa', 'nasdaq/bbb', 'nasdaq/ccc']]}}

            writer.add_custom_scalars(layout)
        """
        self._get_file_writer().add_summary(custom_scalars(layout))

    def add_mesh(self, tag, vertices, colors=None, faces=None, config_dict=None, global_step=None, walltime=None):
        """Add meshes or 3D point clouds to TensorBoard. The visualization is based on Three.js,
        so it allows users to interact with the rendered object. Besides the basic definitions
        such as vertices, faces, users can further provide camera parameter, lighting condition, etc.
        Please check https://threejs.org/docs/index.html#manual/en/introduction/Creating-a-scene for
        advanced usage. Note that currently this depends on tb-nightly to show.

        Args:
            tag (string): Data identifier
            vertices (torch.Tensor): List of the 3D coordinates of vertices.
            colors (torch.Tensor): Colors for each vertex
            faces (torch.Tensor): Indices of vertices within each triangle. (Optional)
            config_dict: Dictionary with ThreeJS classes names and configuration.
            global_step (int): Global step value to record
            walltime (float): Optional override default walltime (time.time())
              seconds after epoch of event

        Shape:
            vertices: :math:`(B, N, 3)`. (batch, number_of_vertices, channels). If you see nothing on
              tensorboard, try normalizing the values to [-1, 1].

            colors: :math:`(B, N, 3)`. The values should lie in [0, 255].

            faces: :math:`(B, N, 3)`. The values should lie in [0, number_of_vertices] for type `uint8`.

        Examples::

            from tensorboardX import SummaryWriter
            vertices_tensor = np.array([[
                [1, 1, 1],
                [-1, -1, 1],
                [1, -1, -1],
                [-1, 1, -1],
            ]], dtype=float)
            colors_tensor = np.array([[
                [255, 0, 0],
                [0, 255, 0],
                [0, 0, 255],
                [255, 0, 255],
            ]], dtype=int)
            faces_tensor = np.array([[
                [0, 2, 3],
                [0, 3, 1],
                [0, 1, 2],
                [1, 3, 2],
            ]], dtype=int)

            writer = SummaryWriter()
            writer.add_mesh('my_mesh', vertices=vertices_tensor, colors=colors_tensor, faces=faces_tensor)

            writer.close()
        """
        self._get_file_writer().add_summary(mesh(tag, vertices, colors, faces, config_dict), global_step, walltime)

    def close(self):
        if self.all_writers is None:
            return  # ignore double close
        for writer in self.all_writers.values():
            writer.flush()
            writer.close()
        self.file_writer = self.all_writers = None

    def flush(self):
        if self.all_writers is None:
            return  # ignore double close
        for writer in self.all_writers.values():
            writer.flush()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
