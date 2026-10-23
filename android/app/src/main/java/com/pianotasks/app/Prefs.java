package com.pianotasks.app;

import android.content.Context;
import android.content.SharedPreferences;

/** Server address, session token and who we are, kept in SharedPreferences. */
final class Prefs {

    private static final String FILE = "piano_tasks";
    private static final String KEY_SERVER = "server";
    private static final String KEY_TOKEN = "token";
    private static final String KEY_USER = "username";
    private static final String KEY_DISPLAY = "display_name";

    private final SharedPreferences sp;

    Prefs(Context context) {
        this.sp = context.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    String server() { return sp.getString(KEY_SERVER, ApiClient.DEFAULT_SERVER); }
    void setServer(String value) { sp.edit().putString(KEY_SERVER, value).apply(); }

    String token() { return sp.getString(KEY_TOKEN, ""); }
    void setToken(String value) { sp.edit().putString(KEY_TOKEN, value == null ? "" : value).apply(); }

    String username() { return sp.getString(KEY_USER, ""); }
    void setUsername(String value) { sp.edit().putString(KEY_USER, value == null ? "" : value).apply(); }

    String display() { return sp.getString(KEY_DISPLAY, ""); }
    void setDisplay(String value) { sp.edit().putString(KEY_DISPLAY, value == null ? "" : value).apply(); }

    void signOut() {
        sp.edit().remove(KEY_TOKEN).remove(KEY_USER).remove(KEY_DISPLAY).apply();
    }
}