package com.pianotasks.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DatePickerDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.DialogInterface;
import android.graphics.Paint;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.DatePicker;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The whole app: one screen for signing in, one for the task list.
 *
 * Deliberately plain - framework widgets only, no AndroidX, no RecyclerView.
 * The lists are short enough that rebuilding the rows on each refresh is
 * simpler (and less bug-prone) than maintaining an adapter.
 */
public class MainActivity extends Activity {

    private Prefs prefs;
    private ApiClient api;
    private ExecutorService pool;
    private Handler ui;
    private View current;

    private final List<Map<String, Object>> tasks = new ArrayList<Map<String, Object>>();
    private String serverToday = "";
    private String recurrence = "daily";
    private String pendingDue = null;
    private boolean showArchived = false;
    private int accentColor, warnColor, mutedColor, fgColor;

    // ------------------------------------------------------------ lifecycle

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = new Prefs(this);
        api = new ApiClient(prefs.server());
        api.setToken(prefs.token());
        pool = Executors.newSingleThreadExecutor();
        ui = new Handler(Looper.getMainLooper());

        accentColor = getResources().getColor(R.color.accent, getTheme());
        warnColor = getResources().getColor(R.color.warn, getTheme());
        mutedColor = getResources().getColor(R.color.muted, getTheme());
        fgColor = getResources().getColor(R.color.fg, getTheme());

        if (prefs.token().isEmpty()) {
            showLogin(null);
        } else {
            showMain();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (pool != null) pool.shutdownNow();
    }

    private View f(int id) { return current.findViewById(id); }
    private String s(int id) { return getString(id); }

    // ------------------------------------------------------------ sign in

    private void showLogin(String message) {
        current = LayoutInflater.from(this).inflate(R.layout.activity_login, null);
        setContentView(current);

        final EditText server = (EditText) f(R.id.server_url);
        final EditText username = (EditText) f(R.id.username);
        final EditText password = (EditText) f(R.id.password);

        server.setText(prefs.server());
        username.setText(prefs.username());
        if (message != null) ((TextView) f(R.id.status)).setText(message);

        f(R.id.sign_in).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                authenticate(false, server, username, password);
            }
        });
        f(R.id.register).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                authenticate(true, server, username, password);
            }
        });
    }

    private void authenticate(final boolean creating, EditText serverField,
                              EditText userField, EditText passField) {
        final String server = ApiClient.normalize(serverField.getText().toString());
        final String user = userField.getText().toString().trim().toLowerCase(Locale.US);
        final String pass = passField.getText().toString();

        if (server.isEmpty()) { status("Enter the server address."); return; }
        if (user.isEmpty() || pass.isEmpty()) { status("Enter a username and password."); return; }
        if (creating && pass.length() < 6) {
            status("Password must be at least 6 characters."); return;
        }

        setBusy(true);
        status(creating ? "Creating account…" : "Signing in…");
        prefs.setServer(server);
        api.setBaseUrl(server);

        io(new Work() {
            public Map<String, Object> run() throws Exception {
                return creating ? api.register(user, pass, "") : api.login(user, pass);
            }
        }, new After() {
            public void ok(Map<String, Object> result) {
                Map<String, Object> account = Json.obj(result.get("user"));
                prefs.setToken(Json.str(result.get("token")));
                prefs.setUsername(Json.str(account.get("username")));
                prefs.setDisplay(Json.str(account.get("display_name")));
                setBusy(false);
                showMain();
                toast(creating ? "Welcome!" : "Signed in");
            }

            public void err(Exception e) {
                setBusy(false);
                String message = e.getMessage();
                status(message);
                if (e instanceof ApiClient.ApiException
                        && ((ApiClient.ApiException) e).network) {
                    status(s(R.string.cannot_reach).replace("%1$s", server));
                }
            }
        });
    }

    private void setBusy(boolean busy) {
        if (current == null) return;
        View signIn = current.findViewById(R.id.sign_in);
        if (signIn != null) signIn.setEnabled(!busy);
    }

    private void status(String text) {
        if (current == null) return;
        View view = current.findViewById(R.id.status);
        if (view instanceof TextView) ((TextView) view).setText(text == null ? "" : text);
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }

    // ------------------------------------------------------------ task list

    private void showMain() {
        current = LayoutInflater.from(this).inflate(R.layout.activity_main, null);
        setContentView(current);

        f(R.id.refresh).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { refresh(); }
        });
        f(R.id.menu).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showMenu(); }
        });
        f(R.id.add_btn).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { addTask(); }
        });
        f(R.id.kind_daily).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { setRecurrence("daily"); }
        });
        f(R.id.kind_longterm).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { setRecurrence("none"); }
        });
        f(R.id.due_btn).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { pickDue(); }
        });
        f(R.id.due_btn).setOnLongClickListener(new View.OnLongClickListener() {
            public boolean onLongClick(View v) {
                pendingDue = null;
                setRecurrence("none");
                return true;
            }
        });
        f(R.id.notes_btn).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { toggleNotes(); }
        });

        setRecurrence("daily");
        refresh();
    }

    private void setRecurrence(String value) {
        recurrence = value;
        boolean daily = "daily".equals(value);
        Button dailyBtn = (Button) f(R.id.kind_daily);
        Button longBtn = (Button) f(R.id.kind_longterm);
        dailyBtn.setTextColor(daily ? fgColor : mutedColor);
        longBtn.setTextColor(daily ? mutedColor : fgColor);
        dailyBtn.setBackgroundResource(daily ? R.drawable.segment_bg : R.drawable.btn_outline);
        longBtn.setBackgroundResource(daily ? R.drawable.btn_outline : R.drawable.segment_bg);

        View due = f(R.id.due_btn);
        due.setVisibility(daily ? View.GONE : View.VISIBLE);
        ((Button) due).setText(pendingDue == null
                ? s(R.string.set_due) : "Due " + shortDate(pendingDue));
        if (daily) pendingDue = null;
    }

    private void toggleNotes() {
        View notes = f(R.id.new_notes);
        notes.setVisibility(notes.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE);
        if (notes.getVisibility() == View.VISIBLE) notes.requestFocus();
    }

    private void pickDue() {
        Calendar cal = Calendar.getInstance();
        if (pendingDue != null) {
            Calendar parsed = parseDay(pendingDue);
            if (parsed != null) cal = parsed;
        }
        new DatePickerDialog(this, new DatePickerDialog.OnDateSetListener() {
            public void onDateSet(DatePicker view, int year, int month, int day) {
                pendingDue = String.format(Locale.US, "%04d-%02d-%02d", year, month + 1, day);
                ((Button) f(R.id.due_btn)).setText("Due " + shortDate(pendingDue));
            }
        }, cal.get(Calendar.YEAR), cal.get(Calendar.MONTH), cal.get(Calendar.DAY_OF_MONTH)).show();
    }

    private void addTask() {
        final String title = ((EditText) f(R.id.new_title)).getText().toString().trim();
        if (title.isEmpty()) { status("Type a task first."); return; }
        final String notes = ((EditText) f(R.id.new_notes)).getText().toString();
        final String kind = recurrence;
        final String due = "none".equals(kind) ? pendingDue : null;

        io(new Work() {
            public Map<String, Object> run() throws Exception {
                return api.createTask(title, notes, kind, due);
            }
        }, new After() {
            public void ok(Map<String, Object> result) {
                ((EditText) f(R.id.new_title)).setText("");
                ((EditText) f(R.id.new_notes)).setText("");
                pendingDue = null;
                setRecurrence("daily");
                status("");
                refresh();
            }

            public void err(Exception e) { showError(e); }
        });
    }

    private void refresh() {
        io(new Work() {
            public Map<String, Object> run() throws Exception {
                return api.listTasks(null, showArchived);
            }
        }, new After() {
            public void ok(Map<String, Object> result) {
                serverToday = Json.str(result.get("server_today"));
                tasks.clear();
                for (Object item : Json.arr(result.get("tasks"))) {
                    tasks.add(Json.obj(item));
                }
                render(Json.obj(result.get("summary")));
                status("");
            }

            public void err(Exception e) { showError(e); }
        });
    }

    // ------------------------------------------------------------ rendering

    private void render(Map<String, Object> summary) {
        ((TextView) f(R.id.day_label)).setText(prettyDate(serverToday));
        String who = prefs.display();
        if (who == null || who.isEmpty()) who = prefs.username();
        ((TextView) f(R.id.user_label)).setText("signed in as " + who + " · " + api.getBaseUrl());

        long dailyTotal = Json.num(summary.get("daily_total"));
        long dailyDone = Json.num(summary.get("daily_done"));
        long open = Json.num(summary.get("longterm_open"));
        long overdue = Json.num(summary.get("overdue"));
        long streak = Json.num(summary.get("best_streak"));

        StringBuilder stats = new StringBuilder();
        stats.append(dailyDone).append('/').append(dailyTotal).append(" daily done");
        if (streak > 0) stats.append(" · best streak ").append(streak);
        stats.append(" · ").append(open).append(" open");
        if (overdue > 0) stats.append(" · ").append(overdue).append(" overdue");
        ((TextView) f(R.id.stats)).setText(stats.toString());

        LinearLayout dailyBox = (LinearLayout) f(R.id.daily_container);
        LinearLayout longBox = (LinearLayout) f(R.id.long_container);
        dailyBox.removeAllViews();
        longBox.removeAllViews();

        int dailyCount = 0, longCount = 0;
        for (Map<String, Object> task : tasks) {
            boolean isDaily = "daily".equals(Json.str(task.get("recurrence")));
            if (isDaily) {
                dailyBox.addView(buildRow(task, dailyBox));
                dailyCount++;
            } else {
                longBox.addView(buildRow(task, longBox));
                longCount++;
            }
        }

        View dailyEmpty = f(R.id.daily_empty);
        dailyEmpty.setVisibility(dailyCount == 0 ? View.VISIBLE : View.GONE);
        f(R.id.long_empty).setVisibility(longCount == 0 ? View.VISIBLE : View.GONE);

        if (longCount > 1) {
            // overdue first, then by due date, then oldest first
            List<View> rows = new ArrayList<View>();
            for (int i = 0; i < longBox.getChildCount(); i++) rows.add(longBox.getChildAt(i));
            longBox.removeAllViews();
            java.util.Collections.sort(rows, new java.util.Comparator<View>() {
                public int compare(View a, View b) {
                    Map<String, Object> ta = (Map<String, Object>) a.getTag();
                    Map<String, Object> tb = (Map<String, Object>) b.getTag();
                    int ra = sortRank(ta), rb = sortRank(tb);
                    if (ra != rb) return ra - rb;
                    String da = Json.str(ta.get("due_date"));
                    String db = Json.str(tb.get("due_date"));
                    if (da == null) da = "9999-99-99";
                    if (db == null) db = "9999-99-99";
                    int byDue = da.compareTo(db);
                    if (byDue != 0) return byDue;
                    return (int) (Json.num(ta.get("id")) - Json.num(tb.get("id")));
                }
            });
            for (View row : rows) longBox.addView(row);
        }
    }

    private int sortRank(Map<String, Object> task) {
        if (Json.bool(task.get("archived"))) return 2;
        if (Json.bool(task.get("done"))) return 1;
        return 0;
    }

    private View buildRow(final Map<String, Object> task, ViewGroup parent) {
        View row = LayoutInflater.from(this).inflate(R.layout.row_task, parent, false);
        row.setTag(task);

        final long id = Json.num(task.get("id"));
        final boolean isDaily = "daily".equals(Json.str(task.get("recurrence")));
        final boolean done = Json.bool(task.get("done"));

        TextView title = (TextView) row.findViewById(R.id.title);
        title.setText(Json.str(task.get("title")));
        if (done) {
            title.setTextColor(mutedColor);
            title.setPaintFlags(title.getPaintFlags() | Paint.STRIKE_THRU_TEXT_FLAG);
        } else {
            title.setTextColor(fgColor);
        }

        Button check = (Button) row.findViewById(R.id.check);
        check.setBackgroundResource(done ? R.drawable.check_on : R.drawable.check_off);
        check.setText(done ? "✓" : "");
        check.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { toggle(task); }
        });

        TextView meta = (TextView) row.findViewById(R.id.meta);
        meta.setText(metaText(task, isDaily, done));
        if (!done && Json.bool(task.get("overdue"))) meta.setTextColor(warnColor);
        else meta.setTextColor(mutedColor);

        row.findViewById(R.id.more).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { taskDialog(task); }
        });
        row.setOnLongClickListener(new View.OnLongClickListener() {
            public boolean onLongClick(View v) { taskDialog(task); return true; }
        });

        LinearLayout strip = (LinearLayout) row.findViewById(R.id.strip);
        List<Object> history = Json.arr(task.get("history"));
        if (isDaily && !history.isEmpty()) {
            strip.setVisibility(View.VISIBLE);
            for (Object flag : history) {
                strip.addView(stripBar(Json.bool(flag)));
            }
        } else {
            strip.setVisibility(View.GONE);
        }
        return row;
    }

    private String metaText(Map<String, Object> task, boolean isDaily, boolean done) {
        StringBuilder meta = new StringBuilder();
        if (isDaily) {
            long streak = Json.num(task.get("streak"));
            long total = Json.num(task.get("total_done"));
            if (streak > 0) meta.append("🔥 ").append(streak).append(" day streak · ");
            meta.append(total).append(" total");
        } else if (done) {
            meta.append(s(R.string.done_label));
        } else {
            String due = Json.str(task.get("due_date"));
            meta.append(due == null ? "no due date" : dueText(due));
        }
        if (Json.bool(task.get("archived"))) meta.append(" · archived");
        return meta.toString();
    }

    private View stripBar(boolean on) {
        View bar = new View(this);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(5), 1f);
        params.rightMargin = dp(3);
        bar.setLayoutParams(params);
        GradientDrawable shape = new GradientDrawable();
        shape.setCornerRadius(dp(3));
        shape.setColor(getResources().getColor(on ? R.color.strip_on : R.color.strip_off,
                getTheme()));
        bar.setBackground(shape);
        return bar;
    }

    // ------------------------------------------------------------ mutations

    private void toggle(final Map<String, Object> task) {
        final long id = Json.num(task.get("id"));
        final boolean isDaily = "daily".equals(Json.str(task.get("recurrence")));
        io(new Work() {
            public Map<String, Object> run() throws Exception {
                return api.toggleTask(id, isDaily ? serverToday : null);
            }
        }, afterRefresh());
    }

    private void taskDialog(final Map<String, Object> task) {
        final long id = Json.num(task.get("id"));
        final boolean archived = Json.bool(task.get("archived"));
        final String[] items = new String[] {
                s(R.string.edit_title),
                archived ? s(R.string.archive_off) : s(R.string.archive_on),
                s(R.string.delete_task)
        };
        new AlertDialog.Builder(this)
                .setTitle(Json.str(task.get("title")))
                .setItems(items, new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        if (which == 0) renameDialog(task);
                        else if (which == 1) setArchived(id, !archived);
                        else confirmDelete(task);
                    }
                })
                .setNegativeButton(s(R.string.cancel), null)
                .show();
    }

    private void renameDialog(final Map<String, Object> task) {
        final long id = Json.num(task.get("id"));
        final EditText field = new EditText(this);
        field.setInputType(InputType.TYPE_CLASS_TEXT);
        field.setText(Json.str(task.get("title")));
        field.setSelection(field.getText().length());

        new AlertDialog.Builder(this)
                .setTitle(s(R.string.edit_title))
                .setView(field)
                .setPositiveButton(s(R.string.save), new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        final String title = field.getText().toString().trim();
                        if (title.isEmpty()) return;
                        io(new Work() {
                            public Map<String, Object> run() throws Exception {
                                return api.renameTask(id, title);
                            }
                        }, afterRefresh());
                    }
                })
                .setNegativeButton(s(R.string.cancel), null)
                .show();
    }

    private void setArchived(final long id, final boolean archived) {
        io(new Work() {
            public Map<String, Object> run() throws Exception {
                return api.setArchived(id, archived);
            }
        }, afterRefresh());
    }

    private void confirmDelete(final Map<String, Object> task) {
        final long id = Json.num(task.get("id"));
        new AlertDialog.Builder(this)
                .setMessage(String.format(s(R.string.confirm_delete), Json.str(task.get("title"))))
                .setPositiveButton(s(R.string.delete_task), new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        io(new Work() {
                            public Map<String, Object> run() throws Exception {
                                return api.deleteTask(id);
                            }
                        }, afterRefresh());
                    }
                })
                .setNegativeButton(s(R.string.cancel), null)
                .show();
    }

    private After afterRefresh() {
        return new After() {
            public void ok(Map<String, Object> result) { refresh(); }
            public void err(Exception e) { showError(e); }
        };
    }

    // ------------------------------------------------------------ menu

    private void showMenu() {
        final String[] items = new String[] {
                (showArchived ? "Hide archived tasks" : "Show archived tasks"),
                s(R.string.export_json),
                s(R.string.change_server),
                s(R.string.sign_out)
        };
        new AlertDialog.Builder(this)
                .setItems(items, new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        if (which == 0) {
                            showArchived = !showArchived;
                            refresh();
                        } else if (which == 1) {
                            exportToClipboard();
                        } else if (which == 2) {
                            changeServerDialog();
                        } else {
                            signOut();
                        }
                    }
                })
                .show();
    }

    private void exportToClipboard() {
        io(new Work() {
            public Map<String, Object> run() throws Exception {
                Map<String, Object> wrapper = new LinkedHashMap<String, Object>();
                wrapper.put("json", api.exportJson());
                return wrapper;
            }
        }, new After() {
            public void ok(Map<String, Object> result) {
                ClipboardManager clipboard =
                        (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                clipboard.setPrimaryClip(ClipData.newPlainText("Piano Tasks export",
                        Json.str(result.get("json"))));
                toast("Tasks copied to the clipboard");
            }

            public void err(Exception e) { showError(e); }
        });
    }

    private void changeServerDialog() {
        final EditText field = new EditText(this);
        field.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        field.setText(prefs.server());
        field.setSelection(field.getText().length());
        new AlertDialog.Builder(this)
                .setTitle(s(R.string.change_server))
                .setView(field)
                .setPositiveButton(s(R.string.save), new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        String url = ApiClient.normalize(field.getText().toString());
                        if (url.isEmpty()) return;
                        prefs.setServer(url);
                        api.setBaseUrl(url);
                        refresh();
                    }
                })
                .setNegativeButton(s(R.string.cancel), null)
                .show();
    }

    private void signOut() {
        new AlertDialog.Builder(this)
                .setMessage(s(R.string.sign_out) + "?")
                .setPositiveButton(s(R.string.sign_out), new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        io(new Work() {
                            public Map<String, Object> run() throws Exception {
                                api.logout();
                                return null;
                            }
                        }, new After() {
                            public void ok(Map<String, Object> result) {
                                prefs.signOut();
                                tasks.clear();
                                showLogin("Signed out.");
                            }

                            public void err(Exception e) {
                                prefs.signOut();
                                tasks.clear();
                                showLogin("Signed out.");
                            }
                        });
                    }
                })
                .setNegativeButton(s(R.string.cancel), null)
                .show();
    }

    // ------------------------------------------------------------ plumbing

    private interface Work {
        Map<String, Object> run() throws Exception;
    }

    private interface After {
        void ok(Map<String, Object> result);
        void err(Exception e);
    }

    private void io(final Work work, final After after) {
        pool.execute(new Runnable() {
            public void run() {
                try {
                    final Map<String, Object> result = work.run();
                    ui.post(new Runnable() {
                        public void run() { after.ok(result); }
                    });
                } catch (final Exception e) {
                    ui.post(new Runnable() {
                        public void run() { after.err(e); }
                    });
                }
            }
        });
    }

    private void showError(Exception e) {
        if (e instanceof ApiClient.ApiException) {
            ApiClient.ApiException api_error = (ApiClient.ApiException) e;
            if (api_error.network) {
                status(s(R.string.cannot_reach).replace("%1$s", api.getBaseUrl()));
                toast("Cannot reach " + api.getBaseUrl());
                return;
            }
            if (api_error.status == 401 || api_error.status == 403) {
                prefs.signOut();
                showLogin("Your session ended — please sign in again.");
                return;
            }
            status(api_error.getMessage());
            toast(api_error.getMessage());
            return;
        }
        status(String.valueOf(e.getMessage()));
    }

    // ------------------------------------------------------------ dates

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private Calendar parseDay(String day) {
        if (day == null || day.length() < 10) return null;
        try {
            Calendar cal = Calendar.getInstance();
            cal.set(Integer.parseInt(day.substring(0, 4)),
                    Integer.parseInt(day.substring(5, 7)) - 1,
                    Integer.parseInt(day.substring(8, 10)), 0, 0, 0);
            cal.set(Calendar.MILLISECOND, 0);
            return cal;
        } catch (RuntimeException e) {
            return null;
        }
    }

    private String prettyDate(String day) {
        Calendar cal = parseDay(day);
        if (cal == null) return s(R.string.app_name);
        return new SimpleDateFormat("EEEE, MMMM d", Locale.getDefault()).format(cal.getTime());
    }

    private String shortDate(String day) {
        Calendar cal = parseDay(day);
        if (cal == null) return day;
        return new SimpleDateFormat("MMM d", Locale.getDefault()).format(cal.getTime());
    }

    private long daysBetween(String from, String to) {
        Calendar a = parseDay(from), b = parseDay(to);
        if (a == null || b == null) return 0;
        long ms = b.getTimeInMillis() - a.getTimeInMillis();
        return Math.round(ms / 86400000.0);
    }

    private String dueText(String due) {
        long days = daysBetween(serverToday, due);
        if (days < 0) return (days == -1 ? "1 day overdue" : (-days) + " days overdue");
        if (days == 0) return s(R.string.due_today);
        if (days == 1) return s(R.string.due_tomorrow);
        return "due " + shortDate(due);
    }
}