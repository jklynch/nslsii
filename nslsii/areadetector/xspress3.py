from collections import OrderedDict
import functools
import logging
import time

from ophyd.areadetector import EpicsSignalWithRBV as SignalWithRBV
from ophyd import Signal, EpicsSignal, EpicsSignalRO, DerivedSignal

from ophyd import (
    Component as Cpt,
    FormattedComponent as FC,  # noqa: F401
    DynamicDeviceComponent as DynamicDeviceCpt,
)
from ophyd.areadetector.filestore_mixins import FileStorePluginBase

from ophyd.areadetector.plugins import HDF5Plugin
from ophyd.areadetector import ADBase
from ophyd.areadetector import Xspress3Detector
from ophyd.device import BlueskyInterface, Staged
from ophyd.status import DeviceStatus

from databroker.assets.handlers import (
    Xspress3HDF5Handler,
    # XS3_XRF_DATA_KEY as XRF_DATA_KEY,
)

from ..detectors.utils import makedirs


logger = logging.getLogger(__name__)


def ev_to_bin(ev):
    """Convert eV to bin number"""
    return int(ev / 10)


def bin_to_ev(bin_):
    """Convert bin number to eV"""
    return int(bin_) * 10


class EvSignal(DerivedSignal):
    """A signal that converts a bin number into electron volts"""

    def __init__(self, parent_attr, *, parent=None, **kwargs):
        bin_signal = getattr(parent, parent_attr)
        super().__init__(derived_from=bin_signal, parent=parent, **kwargs)

    def get(self, **kwargs):
        bin_ = super().get(**kwargs)
        return bin_to_ev(bin_)

    def put(self, ev_value, **kwargs):
        bin_value = ev_to_bin(ev_value)
        return super().put(bin_value, **kwargs)

    def describe(self):
        desc = super().describe()
        desc[self.name]["units"] = "eV"
        return desc


class Xspress3FileStore(FileStorePluginBase, HDF5Plugin):
    """Xspress3 acquisition -> filestore"""

    num_capture_calc = Cpt(EpicsSignal, "NumCapture_CALC")
    num_capture_calc_disable = Cpt(EpicsSignal, "NumCapture_CALC.DISA")
    filestore_spec = Xspress3HDF5Handler.HANDLER_NAME

    def __init__(
        self,
        basename,
        *,
        config_time=0.5,
        # TODO: is this correct for the new xspress3 IOC?
        mds_key_format="{self.cam.name}_ch{chan}",
        parent=None,
        **kwargs,
    ):
        super().__init__(basename, parent=parent, **kwargs)
        det = parent
        self.cam = det.cam

        # Use the EpicsSignal file_template from the detector
        self.stage_sigs[self.blocking_callbacks] = 1
        self.stage_sigs[self.enable] = 1
        self.stage_sigs[self.compression] = "zlib"
        self.stage_sigs[self.file_template] = "%s%s_%6.6d.h5"

        self._filestore_res = None
        self.channels = list(
            range(1, len([_ for _ in det.component_names if _.startswith("chan")]) + 1)
        )
        # this was in original code, but I kinda-sorta nuked because
        # it was not needed for SRX and I could not guess what it did
        # self._master = None

        self._config_time = config_time
        self.mds_keys = {
            chan: mds_key_format.format(self=self, chan=chan) for chan in self.channels
        }

    def stop(self, success=False):
        ret = super().stop(success=success)
        self.capture.put(0)
        return ret

    def kickoff(self):
        # TODO
        raise NotImplementedError()

    def collect(self):
        # TODO (hxn-specific implementation elsewhere)
        raise NotImplementedError()

    def make_filename(self):
        fn, rp, write_path = super().make_filename()
        if self.parent.make_directories.get():
            makedirs(write_path)
        return fn, rp, write_path

    def unstage(self):
        try:
            i = 0
            # this needs a fail-safe, RE will now hang forever here
            # as we eat all SIGINT to ensure that cleanup happens in
            # orderly manner.
            # If we are here this is a sign that we have not configured the xs3
            # correctly and it is expecting to capture more points than it
            # was triggered to take.
            while self.capture.get() == 1:
                i += 1
                if (i % 50) == 0:
                    logger.warning("Still capturing data .... waiting.")
                time.sleep(0.1)
                if i > 150:
                    logger.warning("Still capturing data .... giving up.")
                    logger.warning(
                        "Check that the xspress3 is configured to take the right "
                        "number of frames "
                        f"(it is trying to take {self.parent.cam.num_images.get()})"
                    )
                    self.capture.put(0)
                    break

        except KeyboardInterrupt:
            self.capture.put(0)
            logger.warning("Still capturing data .... interrupted.")

        return super().unstage()

    def generate_datum(self, key, timestamp, datum_kwargs):
        sn, n = next(
            (f"channel{j}", j)
            for j in self.channels
            if getattr(self.parent, f"channel{j}").name == key
        )
        datum_kwargs.update(
            {"frame": self.parent._abs_trigger_count, "channel": int(sn[7:])}
        )
        self.mds_keys[n] = key
        super().generate_datum(key, timestamp, datum_kwargs)

    def stage(self):
        # if should external trigger
        external_trig_reading = self.parent.external_trig.get()

        logger.debug("Stopping xspress3 acquisition")
        # really force it to stop acquiring
        self.cam.acquire.put(0, wait=True)

        total_points_reading = self.parent.total_points.get()
        if total_points_reading < 1:
            raise RuntimeError("You must set the total points")
        spec_per_point = self.parent.spectra_per_point.get()
        total_capture = total_points_reading * spec_per_point

        # stop previous acquisition
        self.stage_sigs[self.cam.acquire] = 0

        # re-order the stage signals and disable the calc record which is
        # interfering with the capture count
        self.stage_sigs.pop(self.num_capture, None)
        self.stage_sigs.pop(self.cam.num_images, None)
        self.stage_sigs[self.num_capture_calc_disable] = 1

        if external_trig_reading:
            logger.debug("Setting up external triggering")
            self.stage_sigs[self.cam.trigger_mode] = "TTL Veto Only"
            # self.stage_sigs[self.cam.trigger_mode] = "Internal"
            self.stage_sigs[self.cam.num_images] = total_capture
        else:
            logger.debug("Setting up internal triggering")
            # self.cam.trigger_mode.put('Internal')
            # self.cam.num_images.put(1)
            self.stage_sigs[self.cam.trigger_mode] = "Internal"
            self.stage_sigs[self.cam.num_images] = spec_per_point

        self.stage_sigs[self.auto_save] = "No"
        logger.debug("Configuring other filestore stuff")

        logger.debug("Making the filename")
        filename, read_path, write_path = self.make_filename()

        logger.debug(
            "Setting up hdf5 plugin: ioc path: %s filename: %s", write_path, filename
        )

        logger.debug("Erasing old spectra")
        self.cam.erase.put(1, wait=True)

        # this must be set after self.cam.num_images because at the Epics
        # layer  there is a helpful link that sets this equal to that (but
        # not the other way)
        self.stage_sigs[self.num_capture] = total_capture

        # actually apply the stage_sigs
        ret = super().stage()

        self._fn = self.file_template.get() % (
            self._fp,
            self.file_name.get(),
            self.file_number.get(),
        )

        if not self.file_path_exists.get():
            raise IOError(
                "Path {} does not exits on IOC!! Please Check".format(
                    self.file_path.get()
                )
            )

        logger.debug("Inserting the filestore resource: %s", self._fn)
        self._generate_resource({})
        self._filestore_res = self._asset_docs_cache[-1][-1]

        # this gets auto turned off at the end
        self.capture.put(1)

        # Xspress3 needs a bit of time to configure itself...
        # this does not play nice with the event loop :/
        time.sleep(self._config_time)

        return ret

    def configure(self, total_points=0, master=None, external_trig=False, **kwargs):
        raise NotImplementedError()

    def describe(self):
        # should this use a better value?
        size = (self.width.get(),)

        spec_desc = {
            "external": "FILESTORE:",
            "dtype": "array",
            "shape": size,
            "source": "FileStore:",
        }

        desc = OrderedDict()
        for chan in self.channels:
            key = self.mds_keys[chan]
            desc[key] = spec_desc

        return desc


# start new IOC classes
# are these general areadetector plugins?
# but for now they are being used just for xspress3
class Mca(ADBase):
    array_data = Cpt(EpicsSignal, "ArrayData")


class McaSum(ADBase):
    array_data = Cpt(EpicsSignal, "ArrayData")


class McaRoi(ADBase):
    roi_name = Cpt(EpicsSignal, "Name")
    min_x = Cpt(EpicsSignal, "MinX")
    size_x = Cpt(EpicsSignal, "SizeX")
    total_rbv = Cpt(EpicsSignalRO, "Total_RBV")

    use = Cpt(SignalWithRBV, "Use")

    def configure_roi(self, ev_min, ev_size):
        """Configure the ROI with min and size eV

        Parameters
        ----------
        ev_min : int
            minimum electron volts for ROI
        ev_size : int
            ROI size (width) in electron volts
        """
        ev_min = int(ev_min)
        ev_size = int(ev_size)

        # assume if this ROI is being configured
        # that it should be read, meaning the
        # "use" PV must be set to 1
        use_roi = 1
        configuration_changed = any(
            [
                self.min_x.get() != ev_min,
                self.size_x.get() != ev_size,
                self.use.get() != use_roi,
            ]
        )

        if configuration_changed:
            logger.debug(
                "Setting up Xspress3 ROI: name=%s ev_min=%s ev_size=%s "
                "use=%s prefix=%s channel=%s",
                self.name,
                ev_min,
                ev_size,
                use_roi,
                self.prefix,
                # self.parent is the ?? class
                # self.parent.parent is the ?? class
                # TODO: I don't like the assumption that self has a parent
                self.parent.parent.channel_num,
            )

            self.min_x.put(ev_min)
            self.size_x.put(ev_size)
            self.use.put(use_roi)
        else:
            # nothing has changed
            pass

    def clear(self):
        """Clear and disable this ROI"""
        # it is enough to just disable the ROI
        # self.min_x.put(0)
        # self.size_x.put(0)
        self.use.put(0)


class Sca(ADBase):
    # includes Dead Time correction, for example
    # sca numbers go from 0 to 10
    clock_ticks = Cpt(EpicsSignalRO, "0:Value_RBV")
    reset_ticks = Cpt(EpicsSignalRO, "1:Value_RBV")
    reset_counts = Cpt(EpicsSignalRO, "2:Value_RBV")
    all_event = Cpt(EpicsSignalRO, "3:Value_RBV")
    all_good = Cpt(EpicsSignalRO, "4:Value_RBV")
    window_1 = Cpt(EpicsSignalRO, "5:Value_RBV")
    window_2 = Cpt(EpicsSignalRO, "6:Value_RBV")
    pileup = Cpt(EpicsSignalRO, "7:Value_RBV")
    event_width = Cpt(EpicsSignalRO, "8:Value_RBV")
    dt_factor = Cpt(EpicsSignalRO, "9:Value_RBV")
    dt_percent = Cpt(EpicsSignalRO, "10:Value_RBV")


class Xspress3ChannelBase(ADBase):
    """"""

    roi_name_format = "Det{self.channel_num}_{roi_name}"
    roi_total_name_format = "Det{self.channel_num}_{roi_name}_total"

    def __init__(self, prefix, *args, **kwargs):
        super().__init__(prefix, *args, **kwargs)

    def set_roi(self, index_or_roi, *, ev_min, ev_size, name=None):
        """Configure MCAROI with energy range and optionally name.

        Parameters
        ----------
        index_or_roi : int
            The roi index or instance to set
        ev_min : int
            low eV setting
        ev_size : int
            roi width eV setting
        name : str, optional
            The unformatted ROI name to set. Each channel specifies its own
            `roi_name_format` and `roi_sum_name_format` in which the name
            parameter will get expanded.
        """
        if isinstance(index_or_roi, McaRoi):
            roi = index_or_roi
        else:
            if index_or_roi <= 0:
                raise ValueError("MCAROI index starts from 1")
            roi = getattr(self.mcarois, f"mcaroi{index_or_roi:02d}")

        roi.configure_roi(ev_min, ev_size)

        if name is not None:
            roi_name = self.roi_name_format.format(self=self, roi_name=name)
            roi.roi_name.name = roi_name
            roi.total_rbv.name = self.roi_total_name_format.format(
                self=self, roi_name=roi_name
            )

    def clear_all_rois(self):
        """Clear all ROIs"""
        for roi in self.mca_rois:
            roi.clear()


# cache returned class objects to avoid
# building redundant classes
@functools.lru_cache
def build_channel_class(channel_num, roi_count):
    """Build an Xspress3 channel class with the specified channel number and ROI count.

    The complication of using dynamically generated classes
    is the price for the relative ease of including the channel
    number in MCAROI PVs and the ability to specify the number
    of ROIs that will be used rather than defaulting to the
    maximum of 48 per channel.

    Parameters
    ----------
    channel_num: int
        the channel number, 1-16
    roi_count: int
        the number of MCAROI PVs, 1-48

    Returns
    -------
    a dynamically generated class similar to this:
        class Xspress3Channel_2_with_4_rois(Xspress3ChannelBase):
            channel_num = 2
            sca = Sca(...)
            mca = Mca(...)
            mca_sum = McaSum(...)
            mcarois = DDC(...4 McaRois...)

    """

    return type(
        f"Xspress3Channel_{channel_num}_with_{roi_count}_rois",
        (Xspress3ChannelBase,),
        {
            "channel_num": channel_num,
            "sca": Cpt(Sca, f"C{channel_num}SCA:"),
            "mca": Cpt(Mca, f"MCA{channel_num}:"),
            "mca_sum": Cpt(McaSum, f"MCA{channel_num}SUM:"),
            "mcarois": DynamicDeviceCpt(
                defn=OrderedDict(
                    {
                        f"mcaroi{mcaroi_i:02d}": (
                            McaRoi,
                            # MCAROI PV names look like "MCA1ROI:2:"
                            f"MCA{channel_num}ROI:{mcaroi_i:d}:",
                            # no keyword parameters
                            dict(),
                        )
                        for mcaroi_i in range(1, roi_count + 1)
                    }
                )
            ),
        },
    )


# cache returned class objects to
# avoid building redundant classes
@functools.lru_cache
def build_detector_class(channel_count, roi_count, parent_classes=None):
    """Build an Xspress3 detector class with the specified number of channels and rois.

    The complication of using dynamically generated detector classes
    is the price for being able to easily specify the exact number of
    channels and ROIs per channel present on the detector.

    TODO: TOM, can we get rid of these soft signals by rewriting Xspress3FileStore?
    Detector classes generated by this function include these "soft" signals
    which are not part of the Xspress3 IOC but are used by the Xspress3FileStore:
        external_trig
        total_points
        spectra_per_point
        make_directories
        rewindable

    Parameters
    ----------
    channel_count: int
        number of channels for the detector, 1-16
    roi_count: int
        number of ROIs per channel, 1-48
    parent_classes: list-like, optional
        list of parent classes for the generated detector class to be
        included with ophyd.areadetector.Xspress3Detector

    Returns
    -------
    a dynamically generated class similar to this:
        class Xspress3Detector_4_channel_3roi(Xspress3Detector, SomeMixinClass, ...):
            external_trig = Cpt(Signal, value=False)
            total_points = Cpt(Signal, value=-1)
            spectra_per_point = Cpt(Signal, value=1)
            make_directories = Cpt(Signal, value=False)
            rewindable = Cpt(Signal, value=False)
            channels = DDC(...4 Xspress3Channels with 3 ROIs each...)
    """
    if parent_classes is None:
        parent_classes = []

    return type(
        f"Xspress3Detector_{channel_count}channel_{roi_count}roi",
        (Xspress3Detector, *parent_classes),
        {
            "external_trig": Cpt(Signal, value=False, doc="Use external triggering"),
            "total_points": Cpt(
                Signal, value=-1, doc="The total number of points to acquire overall"
            ),
            "spectra_per_point": Cpt(
                Signal, value=1, doc="Number of spectra per point"
            ),
            "make_directories": Cpt(
                Signal, value=False, doc="Make directories on the DAQ side"
            ),
            "rewindable": Cpt(
                Signal, value=False, doc="Xspress3 cannot safely be rewound in bluesky"
            ),
            "channels": DynamicDeviceCpt(
                defn=OrderedDict(
                    {
                        f"channel_{c}": (
                            build_channel_class(channel_num=c, roi_count=roi_count),
                            # there is no discrete Xspress3 channel prefix
                            # so specify an empty string here
                            "",
                            dict(),
                        )
                        for c in range(1, channel_count + 1)
                    }
                )
            ),
        },
    )


# end new IOC classes


class XspressTrigger(BlueskyInterface):
    """Base class for trigger mixin classes

    Subclasses must define a method with this signature:

    `acquire_changed(self, value=None, old_value=None, **kwargs)`
    """

    # TODO **
    # count_time = self.cam.acquire_period

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # settings
        self._status = None
        self._acquisition_signal = self.cam.acquire
        self._abs_trigger_count = 0

    def stage(self):
        self._abs_trigger_count = 0
        self._acquisition_signal.subscribe(self._acquire_changed)
        return super().stage()

    def unstage(self):
        ret = super().unstage()
        self._acquisition_signal.clear_sub(self._acquire_changed)
        self._status = None
        return ret

    def _acquire_changed(self, value=None, old_value=None, **kwargs):
        "This is called when the 'acquire' signal changes."
        if self._status is None:
            return
        if (old_value == 1) and (value == 0):
            # Negative-going edge means an acquisition just finished.
            self._status._finished()

    def trigger(self):
        if self._staged != Staged.yes:
            raise RuntimeError("not staged")

        self._status = DeviceStatus(self)
        self._acquisition_signal.put(1, wait=False)
        trigger_time = time.time()

        for sn in self.read_attrs:
            if sn.startswith("channel") and "." not in sn:
                ch = getattr(self, sn)
                self.dispatch(ch.name, trigger_time)

        self._abs_trigger_count += 1
        return self._status
