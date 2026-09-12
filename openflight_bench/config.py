"""Generate the one OpenFlight bench waveform and its controlled variants."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib

TX_MODES = {
    "all": (1, 2, 4),
    "off": (0, 0, 0),
    "tx0": (1, 0, 0),
    "tx1": (0, 2, 0),
    "tx2": (0, 0, 4),
    "tx02": (1, 0, 4),
}


@dataclass
class BenchProfile:
    """RF/capture settings that can be changed without editing a CFG file.

    ``txbackoff`` is the per-transmitter code in dB. The mmWave CLI expects
    one packed 8-bit code per TX in the profileCfg backoff word; it does not
    expect the human number 6 as that field's decimal value.
    """

    name: str = "baseline_rx24_tx6_hpf00"
    tx_mode: str = "all"
    rxgain: int = 24
    txbackoff: int = 6
    hpf1: int = 0
    hpf2: int = 0
    start_bin: int = 0
    bin_count: int = 43
    post_frames: int = 30
    post_stride: int = 1
    loops: int = 12
    num_adc_samples: int = 128
    sample_rate_ksps: int = 4000
    slope_mhz_per_us: float = 100.0
    start_freq_ghz: float = 60.0
    # TI's frameCfg periodicity is expressed as a whole number of milliseconds.
    frame_period_ms: int = 3

    def __post_init__(self):
        self.tx_mode = normalize_tx_mode(self.tx_mode)
        if not 0 <= self.rxgain <= 48:
            raise ValueError("rxgain must be between 0 and 48 dB")
        if not 0 <= self.txbackoff <= 31:
            raise ValueError("txbackoff must be between 0 and 31 dB code")
        if self.hpf1 not in range(4) or self.hpf2 not in range(4):
            raise ValueError("hpf1 and hpf2 must be 0, 1, 2, or 3")
        if not 1 <= self.bin_count <= self.num_adc_samples:
            raise ValueError("bin_count must be 1..num_adc_samples")
        if not 0 <= self.start_bin <= self.num_adc_samples - self.bin_count:
            raise ValueError("start_bin + bin_count must fit inside the ADC/FFT length")
        if not 1 <= self.post_frames <= 63:
            raise ValueError("post_frames must be 1..63")
        if self.loops < 2 or self.loops > 32 or self.loops % 2:
            raise ValueError("loops must be an even value from 2 through 32")
        if isinstance(self.frame_period_ms, bool):
            raise ValueError("frame_period_ms must be a positive whole number of milliseconds")
        try:
            period = float(self.frame_period_ms)
        except (TypeError, ValueError) as exc:
            raise ValueError("frame_period_ms must be a positive whole number of milliseconds") from exc
        if not period.is_integer() or not 1 <= period <= 65535:
            raise ValueError("frame_period_ms must be a whole number from 1 through 65535")
        self.frame_period_ms = int(period)

    @property
    def tx_masks(self) -> tuple[int, int, int]:
        return TX_MODES[self.tx_mode]

    @property
    def packed_tx_backoff(self) -> int:
        code = self.txbackoff & 0xFF
        return code | (code << 8) | (code << 16)

    @property
    def range_bin_m(self) -> float:
        bandwidth_hz = self.slope_mhz_per_us * 1e12 * (
            self.num_adc_samples / (self.sample_rate_ksps * 1e3)
        )
        return 299_792_458.0 / (2.0 * bandwidth_hz)

    def as_dict(self) -> dict:
        result = asdict(self)
        result.update(
            tx_masks=list(self.tx_masks),
            packed_tx_backoff=self.packed_tx_backoff,
            packed_tx_backoff_hex=f"0x{self.packed_tx_backoff:06x}",
            range_bin_m=self.range_bin_m,
        )
        return result

    def copy(self, **changes) -> "BenchProfile":
        values = asdict(self)
        values.update(changes)
        return BenchProfile(**values)


def normalize_tx_mode(value: str) -> str:
    value = value.lower().replace("+", "")
    aliases = {"tx0tx2": "tx02", "tx0_tx2": "tx02", "none": "off"}
    value = aliases.get(value, value)
    if value not in TX_MODES:
        raise ValueError(f"tx mode must be one of: {', '.join(TX_MODES)}")
    return value


def make_config(profile: BenchProfile, *, include_sensor_start: bool = True) -> str:
    """Return the complete CLI configuration for the configurable L3 build."""
    masks = profile.tx_masks
    lines = [
        "dfeDataOutputMode 1",
        "channelCfg 15 7 0",
        "adcCfg 2 1",
        # txOutPowerBackoffCode is the packed 24-bit word. For 6 dB this is
        # 394758 / 0x060606, not the decimal number 6.
        (
            f"profileCfg 0 {profile.start_freq_ghz:.1f} 7 3 38 "
            f"{profile.packed_tx_backoff} 0 {profile.slope_mhz_per_us:g} 1 "
            f"{profile.num_adc_samples} {profile.sample_rate_ksps} "
            f"{profile.hpf1} {profile.hpf2} {profile.rxgain}"
        ),
        f"chirpCfg 0 0 0 0 0 0 0 {masks[0]}",
        f"chirpCfg 1 1 0 0 0 0 0 {masks[1]}",
        f"chirpCfg 2 2 0 0 0 0 0 {masks[2]}",
        f"frameCfg 0 2 {profile.loops} 0 {profile.frame_period_ms} 1 0",
        "captureFormat iq16",
        (
            f"captureCfg {profile.start_bin} {profile.bin_count} "
            f"{profile.start_bin} {profile.bin_count} {profile.start_bin} "
            f"{profile.post_frames} {profile.post_stride}"
        ),
        "lowPower 0 0",
    ]
    if include_sensor_start:
        lines.append("sensorStart")
    return "\n".join(lines) + "\n"


def config_hash(config_text: str) -> str:
    return hashlib.sha256(config_text.encode("ascii")).hexdigest()
