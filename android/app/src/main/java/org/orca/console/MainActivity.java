package org.orca.console;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

/**
 * Orca console for Android. Shows the bundled touch console; use its
 * "Server" button to connect to an Orca server on your network, or
 * "Phone USB radio" to use an RTL-SDR stick plugged into this phone.
 * With neither it runs in demo mode.
 */
public class MainActivity extends Activity {
    private static final int REQ_DRIVER = 4242;
    private static final int DRIVER_PORT = 14423;
    private static final String DRIVER_PKG = "marto.rtl_tcp_andro";

    private WebView web;
    private UsbRadio radio;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setMediaPlaybackRequiresUserGesture(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if ("file".equals(u.getScheme())) return false;
                // capture downloads and outside links open in the phone's browser
                startActivity(new Intent(Intent.ACTION_VIEW, u));
                return true;
            }
        });
        web.addJavascriptInterface(new Bridge(), "OrcaAndroid");
        setContentView(web);
        if (state != null) web.restoreState(state);
        else web.loadUrl("file:///android_asset/console/index.html");
    }

    /** Methods the bundled console can call. Only the local console page is loaded in this WebView. */
    private final class Bridge {
        @JavascriptInterface public boolean hasUsb() { return true; }

        @JavascriptInterface public void startUsb() { runOnUiThread(MainActivity.this::launchDriver); }

        @JavascriptInterface public void stopUsb() { if (radio != null) radio.stop(); }

        @JavascriptInterface public void setView(double lo, double hi) { if (radio != null) radio.setView(lo, hi); }
    }

    /** Ask the RTL-SDR driver app to open the stick and share it on this phone only. */
    private void launchDriver() {
        status("starting", "Opening the RTL-SDR driver… allow USB access if asked");
        Intent i = new Intent(Intent.ACTION_VIEW,
                Uri.parse("iqsrc://-a 127.0.0.1 -p " + DRIVER_PORT + " -s " + UsbRadio.RATE));
        try {
            startActivityForResult(i, REQ_DRIVER);
        } catch (ActivityNotFoundException e) {
            status("nodriver", "Install the free \"RTL-SDR driver\" app, then try again");
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + DRIVER_PKG)));
            } catch (ActivityNotFoundException e2) {
                startActivity(new Intent(Intent.ACTION_VIEW,
                        Uri.parse("https://play.google.com/store/apps/details?id=" + DRIVER_PKG)));
            }
        }
    }

    @Override
    protected void onActivityResult(int req, int result, Intent data) {
        super.onActivityResult(req, result, data);
        if (req != REQ_DRIVER) return;
        if (result != RESULT_OK) {
            String why = data != null ? data.getStringExtra("detailed_exception_message") : null;
            status("error", why != null ? why : "The RTL-SDR driver couldn't open the stick. Check it's plugged in (USB OTG).");
            return;
        }
        if (radio != null) radio.stop();
        radio = new UsbRadio("127.0.0.1", DRIVER_PORT, new UsbRadio.Listener() {
            @Override public void onFrame(double lo, double hi, float[] db, double rbw, boolean single) {
                StringBuilder sb = new StringBuilder(db.length * 7 + 64);
                sb.append("window.orcaUsbFrame&&orcaUsbFrame(").append(lo).append(',').append(hi).append(",[");
                for (int k = 0; k < db.length; k++) {
                    if (k > 0) sb.append(',');
                    sb.append(Math.round(db[k] * 10) / 10.0);
                }
                sb.append("],").append(rbw).append(',').append(single).append(')');
                js(sb.toString());
            }
            @Override public void onStatus(String state, String message) { status(state, message); }
        });
        js("window.orcaUsbReady&&orcaUsbReady()");
        radio.start();
    }

    private void status(String state, String message) {
        js("window.orcaUsbStatus&&orcaUsbStatus(" + JSONObject.quote(state) + "," + JSONObject.quote(message) + ")");
    }

    private void js(String code) {
        web.post(() -> web.evaluateJavascript(code, null));
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onDestroy() {
        if (radio != null) radio.stop();
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }
}
