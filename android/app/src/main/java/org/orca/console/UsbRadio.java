package org.orca.console;

import java.io.BufferedInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Receive-only spectrum source for an RTL-SDR stick plugged into the phone.
 *
 * The stick is driven by the free "RTL-SDR driver" Android app, which shares
 * it on the phone as an rtl_tcp stream (8-bit unsigned I/Q). This class reads
 * that stream, computes power spectra and sweeps across the requested view.
 * It never transmits. Pure Java (no Android APIs) so it can be unit-tested.
 */
public final class UsbRadio implements Runnable {

    /** Receives spectrum frames and status changes. Called from the radio thread. */
    public interface Listener {
        void onFrame(double lo, double hi, float[] db, double rbw, boolean singleTune);
        void onStatus(String state, String message);
    }

    public static final double MIN_HZ = 24e6;      // R820T/R820T2 tuner range
    public static final double MAX_HZ = 1766e6;
    public static final int RATE = 2_048_000;      // samples per second
    public static final int OUT_BINS = 1024;       // points per frame sent to the console
    static final int FFT = 1024;
    static final double USABLE = 0.80;             // keep the flat middle of each capture
    static final int AVG = 8;                      // FFTs averaged per tuning step
    static final float EMPTY_DB = -140f;           // not yet measured / outside tuner range

    private final String host;
    private final int port;
    private final Listener out;
    private volatile boolean running;
    private volatile double viewLo = 1088e6, viewHi = 1092e6;
    private volatile int viewGen = 0;
    private Thread thread;
    private Socket socket;

    public UsbRadio(String host, int port, Listener out) {
        this.host = host; this.port = port; this.out = out;
    }

    public synchronized void start() {
        if (running) return;
        running = true;
        thread = new Thread(this, "orca-usb-radio");
        thread.setDaemon(true);
        thread.start();
    }

    public synchronized void stop() {
        running = false;
        try { if (socket != null) socket.close(); } catch (IOException ignored) { }
        if (thread != null) thread.interrupt();
    }

    public boolean isRunning() { return running; }

    public void setView(double lo, double hi) {
        if (hi <= lo) return;
        viewLo = lo; viewHi = hi; viewGen++;
    }

    @Override public void run() {
        int attempts = 0;
        while (running) {
            try (Socket s = new Socket()) {
                socket = s;
                out.onStatus("connecting", "Connecting to the RTL-SDR driver");
                s.connect(new InetSocketAddress(host, port), 3000);
                s.setSoTimeout(5000);
                s.setReceiveBufferSize(1 << 20);
                attempts = 0;
                stream(new BufferedInputStream(s.getInputStream(), 1 << 18),
                       new DataOutputStream(s.getOutputStream()));
            } catch (IOException e) {
                if (!running) break;
                if (++attempts >= 10) {
                    out.onStatus("error", "Can't reach the RTL-SDR driver. Check the stick is plugged in and allowed.");
                    running = false;
                    break;
                }
                out.onStatus("connecting", "Waiting for the RTL-SDR driver (" + attempts + ")");
                sleep(800);
            }
        }
        out.onStatus("stopped", "USB radio stopped");
    }

    // ------------------------------------------------------------------ stream
    private void stream(InputStream in, DataOutputStream cmd) throws IOException {
        byte[] hdr = new byte[12];
        readFully(in, hdr);
        if (hdr[0] != 'R' || hdr[1] != 'T' || hdr[2] != 'L' || hdr[3] != '0')
            throw new IOException("Not an rtl_tcp stream");
        command(cmd, 0x02, RATE);   // sample rate
        command(cmd, 0x03, 0);      // tuner gain mode: automatic
        command(cmd, 0x08, 1);      // RTL2832 AGC on
        out.onStatus("live", "USB radio live");

        Spectrum sp = new Spectrum(FFT);
        byte[] buf = new byte[FFT * 2 * AVG];
        byte[] skip = new byte[RATE / 12 * 2];           // ~80 ms: flush samples buffered before the retune
        float[] acc = null;
        int gen = -1, step = 0;
        double tunedHz = -1;
        long lastPush = 0;

        while (running) {
            if (gen != viewGen || acc == null) {             // new view: reset the sweep
                gen = viewGen;
                acc = new float[OUT_BINS];
                java.util.Arrays.fill(acc, EMPTY_DB);
                step = 0;
            }
            double lo = viewLo, hi = viewHi;
            double[] centres = plan(lo, hi);
            if (centres.length == 0) {                       // view entirely outside the tuner range
                push(lo, hi, acc, (hi - lo) / OUT_BINS, false);
                sleep(250);
                continue;
            }
            double fc = centres[step % centres.length];
            if (fc != tunedHz) {
                command(cmd, 0x01, (int) Math.round(fc));
                tunedHz = fc;
                readFully(in, skip);                         // let the tuner settle
            }
            readFully(in, buf);
            float[] psd = sp.average(buf, AVG);
            boolean single = centres.length == 1;
            if (single) java.util.Arrays.fill(acc, EMPTY_DB);
            merge(psd, fc, lo, hi, acc, single);
            step++;
            long now = System.currentTimeMillis();
            if (single ? now - lastPush >= 100 : step % centres.length == 0 || now - lastPush > 150) {
                push(lo, hi, acc, Math.max((hi - lo) / OUT_BINS, (double) RATE / FFT), single);
                lastPush = now;
            }
        }
    }

    private void push(double lo, double hi, float[] acc, double rbw, boolean single) {
        out.onFrame(lo, hi, acc.clone(), rbw, single);
    }

    /** Tuning centres needed to cover [lo, hi] within the tuner range. */
    static double[] plan(double lo, double hi) {
        double a = Math.max(lo, MIN_HZ), b = Math.min(hi, MAX_HZ);
        if (b <= a) return new double[0];
        double usable = RATE * USABLE;
        if (b - a <= usable) return new double[] { clampCentre((a + b) / 2) };
        int n = (int) Math.ceil((b - a) / usable);
        double[] c = new double[n];
        for (int i = 0; i < n; i++) c[i] = clampCentre(a + usable * (i + 0.5));
        return c;
    }

    private static double clampCentre(double f) {
        return Math.min(Math.max(f, MIN_HZ), MAX_HZ);
    }

    /** Write one capture's usable bins into the output array (peak-hold per output bin). */
    static void merge(float[] psd, double fc, double lo, double hi, float[] acc, boolean single) {
        int n = psd.length, outN = acc.length;
        double binHz = (double) RATE / n, outHz = (hi - lo) / outN;
        int half = (int) (n * USABLE / 2);
        if (single || outHz < binHz) {
            // narrow view: each output bin takes the nearest FFT bin
            for (int i = 0; i < outN; i++) {
                double f = lo + (i + 0.5) * outHz;
                if (f < MIN_HZ || f > MAX_HZ) continue;
                int k = (int) Math.round((f - fc) / binHz);
                if (k < -half || k > half) continue;
                float v = psd[(k + n) % n];
                if (v > acc[i] || single) acc[i] = v;
            }
            return;
        }
        for (int k = -half; k <= half; k++) {
            double f = fc + k * binHz;
            int i = (int) Math.floor((f - lo) / outHz);
            if (i < 0 || i >= outN || f < MIN_HZ || f > MAX_HZ) continue;
            float v = psd[(k + n) % n];
            if (acc[i] == EMPTY_DB || v > acc[i]) acc[i] = v;
        }
    }

    private static void command(DataOutputStream o, int code, int value) throws IOException {
        o.writeByte(code);
        o.writeInt(value);           // big-endian, as rtl_tcp expects
        o.flush();
    }

    private static void readFully(InputStream in, byte[] b) throws IOException {
        int off = 0;
        while (off < b.length) {
            int r = in.read(b, off, b.length - off);
            if (r < 0) throw new IOException("Stream closed");
            off += r;
        }
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
    }

    // ---------------------------------------------------------------- spectrum
    /** Averaged, windowed power spectrum of 8-bit unsigned I/Q, in dB full scale. */
    static final class Spectrum {
        final int n, bits;
        final double[] win, cos, sin, re, im, pow;
        final double winPower;

        Spectrum(int n) {
            this.n = n;
            this.bits = Integer.numberOfTrailingZeros(n);
            win = new double[n]; cos = new double[n / 2]; sin = new double[n / 2];
            re = new double[n]; im = new double[n]; pow = new double[n];
            double s = 0;
            for (int i = 0; i < n; i++) { win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n); s += win[i] * win[i]; }
            winPower = s;
            for (int i = 0; i < n / 2; i++) { cos[i] = Math.cos(2 * Math.PI * i / n); sin[i] = -Math.sin(2 * Math.PI * i / n); }
        }

        float[] average(byte[] iq, int count) {
            java.util.Arrays.fill(pow, 0);
            for (int c = 0; c < count; c++) {
                int base = c * n * 2;
                for (int i = 0; i < n; i++) {
                    re[i] = (((iq[base + 2 * i] & 0xff) - 127.5) / 127.5) * win[i];
                    im[i] = (((iq[base + 2 * i + 1] & 0xff) - 127.5) / 127.5) * win[i];
                }
                fft(re, im);
                for (int i = 0; i < n; i++) pow[i] += re[i] * re[i] + im[i] * im[i];
            }
            float[] db = new float[n];
            double norm = count * winPower * n;
            for (int i = 0; i < n; i++) db[i] = (float) (10 * Math.log10(pow[i] / norm + 1e-20));
            db[0] = (db[1] + db[n - 1]) / 2;             // hide the DC spike at the tuned centre
            return db;
        }

        private void fft(double[] x, double[] y) {
            for (int i = 0; i < n; i++) {
                int j = Integer.reverse(i) >>> (32 - bits);
                if (j > i) { double t = x[i]; x[i] = x[j]; x[j] = t; t = y[i]; y[i] = y[j]; y[j] = t; }
            }
            for (int size = 2; size <= n; size <<= 1) {
                int halfSize = size >> 1, stepT = n / size;
                for (int i = 0; i < n; i += size) {
                    for (int j = 0; j < halfSize; j++) {
                        double wr = cos[j * stepT], wi = sin[j * stepT];
                        int a = i + j, b = a + halfSize;
                        double tr = x[b] * wr - y[b] * wi, ti = x[b] * wi + y[b] * wr;
                        x[b] = x[a] - tr; y[b] = y[a] - ti;
                        x[a] += tr; y[a] += ti;
                    }
                }
            }
        }
    }
}
