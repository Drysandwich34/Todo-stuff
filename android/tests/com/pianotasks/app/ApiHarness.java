package com.pianotasks.app;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Runs the app's real networking layer (ApiClient + Json) against a live
 * server on a plain JVM. Neither class imports anything from android.*, which
 * is the whole point: the client can be verified without deploying to a device
 * or booting an emulator.
 *
 * Usage:  java -cp <classes> com.pianotasks.app.ApiHarness http://host:8080
 */
public final class ApiHarness {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        String server = args.length > 0 ? args[0] : ApiClient.DEFAULT_SERVER;
        System.out.println("target: " + server);

        jsonChecks();

        try {
            liveChecks(server);
        } catch (Exception e) {
            failed++;
            System.out.println("  FAIL  harness aborted: " + e);
            e.printStackTrace(System.out);
        }

        System.out.println();
        System.out.println(passed + " passed, " + failed + " failed");
        System.exit(failed == 0 ? 0 : 1);
    }

    private static void check(String name, boolean ok, Object detail) {
        if (ok) {
            passed++;
            System.out.println("  PASS  " + name);
        } else {
            failed++;
            System.out.println("  FAIL  " + name + "   <- " + detail);
        }
    }

    private static void check(String name, boolean ok) {
        check(name, ok, null);
    }

    // ------------------------------------------------------------ pure Json

    private static void jsonChecks() {
        System.out.println("\n-- JSON reader/writer");

        Map<String, Object> parsed = Json.obj(Json.parse(
                "{\"s\":\"a\\\"b\\\\c\",\"n\":-12.5,\"t\":true,\"f\":false,\"z\":null,"
                        + "\"a\":[1,2,{\"k\":\"v\"}],\"u\":\"caf\\u00e9\",\"nl\":\"a\\nb\"}"));
        check("parses strings with escapes", "a\"b\\c".equals(Json.str(parsed.get("s"))),
                parsed.get("s"));
        check("parses negative numbers", Json.dbl(parsed.get("n")) == -12.5, parsed.get("n"));
        check("parses booleans", Json.bool(parsed.get("t")) && !Json.bool(parsed.get("f")));
        check("parses null", parsed.get("z") == null);
        check("parses nested arrays", Json.num(Json.obj(Json.arr(parsed.get("a")).get(2)).get("k")) == 0);
        check("parses \\u escapes", "café".equals(Json.str(parsed.get("u"))), parsed.get("u"));
        check("parses embedded newlines", "a\nb".equals(Json.str(parsed.get("nl"))));

        Map<String, Object> nested = new LinkedHashMap<String, Object>();
        nested.put("title", "he said \"hi\"");
        nested.put("count", 3L);
        nested.put("ok", true);
        nested.put("missing", null);
        List<Object> list = new ArrayList<Object>();
        list.add("x");
        list.add(2.0);
        nested.put("list", list);
        Map<String, Object> second = Json.obj(Json.parse(Json.write(nested)));
        check("write -> parse round-trips a string with quotes",
                "he said \"hi\"".equals(Json.str(second.get("title"))), second);
        check("write -> parse round-trips a long as a number",
                Json.num(second.get("count")) == 3L, second.get("count"));
        check("write -> parse round-trips a boolean",
                Json.bool(second.get("ok")));
        check("write emits null", Json.write(null).equals("null"));
        check("write -> parse round-trips a nested list",
                Json.arr(second.get("list")).size() == 2, second.get("list"));

        boolean threw = false;
        try {
            Json.parse("{\"a\":1}trailing");
        } catch (Json.JsonException e) {
            threw = true;
        }
        check("rejects trailing garbage", threw);

        threw = false;
        try {
            Json.parse("{\"a\":}");
        } catch (Json.JsonException e) {
            threw = true;
        }
        check("rejects a missing value", threw);
    }

    // ------------------------------------------------------------ live API

    private static void liveChecks(String server) throws Exception {
        String suffix = Long.toString(System.currentTimeMillis() % 1000000L);
        String username = "harness" + suffix;
        String password = "harness-pass-1";

        System.out.println("\n-- against the live server");
        ApiClient api = new ApiClient(server);
        check("server address is normalised", api.getBaseUrl().startsWith("http"),
                api.getBaseUrl());

        Map<String, Object> health = api.health();
        check("GET /health over the wire", Json.bool(health.get("ok")) && health.get("version") != null,
                health);

        Map<String, Object> registered = api.register(username, password, "Harness");
        check("register a throwaway account", Json.str(registered.get("token")) != null, registered);
        check("the client picked up the token", api.getToken() != null && !api.getToken().isEmpty());

        Map<String, Object> account = Json.obj(api.me().get("user"));
        check("GET /api/me identifies the new account",
                username.equals(Json.str(account.get("username"))), account);

        Map<String, Object> daily = api.createTask("Harness daily", "from the JVM", "daily", null);
        check("create a daily task", "daily".equals(Json.str(daily.get("recurrence"))), daily);
        check("a fresh daily task is unchecked", !Json.bool(daily.get("done")), daily);

        String yesterday = dayOffset(-1);
        Map<String, Object> oneOff =
                api.createTask("Harness long-term", "note", "none", yesterday);
        check("create a long-term task with a past due date",
                yesterday.equals(Json.str(oneOff.get("due_date"))), oneOff);
        check("the past-due task reports overdue", Json.bool(oneOff.get("overdue")), oneOff);

        List<Map<String, Object>> list = api.tasks(null, false);
        check("the list returns both tasks", list.size() == 2, list.size());

        Map<String, Object> summary = Json.obj(api.listTasks(null, false).get("summary"));
        check("summary reports 1 daily, 1 open, 1 overdue",
                Json.num(summary.get("daily_total")) == 1
                        && Json.num(summary.get("longterm_open")) == 1
                        && Json.num(summary.get("overdue")) == 1, summary);

        long dailyId = Json.num(daily.get("id"));
        Map<String, Object> ticked = api.toggleTask(dailyId, null);
        check("toggle the daily task on",
                Json.bool(ticked.get("done")) && Json.num(ticked.get("streak")) == 1, ticked);
        Map<String, Object> unticked = api.toggleTask(dailyId, null);
        check("toggle it back off", !Json.bool(unticked.get("done")), unticked);
        Map<String, Object> backfilled = api.toggleTask(dailyId, dayOffset(-3));
        check("backfill a day three days ago", Json.bool(backfilled.get("done")), backfilled);
        Map<String, Object> again = api.toggleTask(dailyId, null);
        check("ticking today is independent of the backfilled day",
                Json.bool(again.get("done")) && Json.num(again.get("total_done")) == 2, again);
        check("the 14-day strip came back with 14 slots",
                Json.arr(again.get("history")).size() == 14, Json.arr(again.get("history")).size());

        // rename goes through PATCH, which HttpURLConnection cannot send
        Map<String, Object> renamed = api.renameTask(dailyId, "Harness renamed");
        check("PATCH via the method-override header renames the task",
                "Harness renamed".equals(Json.str(renamed.get("title"))), renamed);

        long oneOffId = Json.num(oneOff.get("id"));
        api.setArchived(oneOffId, true);
        List<Map<String, Object>> without = api.tasks(null, false);
        check("archiving removes it from the default list", without.size() == 1, without.size());
        List<Map<String, Object>> with = api.tasks(null, true);
        check("and it comes back when archived tasks are asked for", with.size() == 2, with.size());
        api.setArchived(oneOffId, false);

        Map<String, Object> completed = api.toggleTask(oneOffId, null);
        check("completing a long-term task clears overdue",
                Json.bool(completed.get("done")) && !Json.bool(completed.get("overdue")), completed);

        String dump = api.exportJson();
        check("export is well-formed JSON containing the title",
                dump.contains("Harness renamed") && Json.obj(Json.parse(dump)).get("tasks") != null,
                dump.substring(0, Math.min(80, dump.length())));

        api.deleteTask(dailyId);
        api.deleteTask(oneOffId);
        check("deleting both leaves the list empty", api.tasks(null, true).isEmpty(),
                api.tasks(null, true));

        api.logout();
        boolean rejected = false;
        try {
            api.tasks(null, false);
        } catch (ApiClient.ApiException e) {
            rejected = e.status == 401;
        }
        check("after logout the token is refused with 401", rejected);

        ApiClient anon = new ApiClient(server);
        boolean unauthorised = false;
        try {
            anon.tasks(null, false);
        } catch (ApiClient.ApiException e) {
            unauthorised = e.status == 401;
        }
        check("an unauthenticated client is refused with 401", unauthorised);

        boolean badPassword = false;
        try {
            anon.login(username, "definitely-wrong");
        } catch (ApiClient.ApiException e) {
            badPassword = e.status == 401;
        }
        check("a wrong password is refused with 401", badPassword);

        ApiClient nowhere = new ApiClient("http://127.0.0.1:9");
        boolean network = false;
        try {
            nowhere.health();
        } catch (ApiClient.ApiException e) {
            network = e.network;
        }
        check("an unreachable server is reported as a network error, not a crash", network);
    }

    private static String dayOffset(int days) {
        java.util.Calendar cal = java.util.Calendar.getInstance();
        cal.add(java.util.Calendar.DAY_OF_MONTH, days);
        return String.format(java.util.Locale.US, "%04d-%02d-%02d",
                cal.get(java.util.Calendar.YEAR),
                cal.get(java.util.Calendar.MONTH) + 1,
                cal.get(java.util.Calendar.DAY_OF_MONTH));
    }
}