package com.pianotasks.app;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Talks to the Piano Tasks server.
 *
 * Imports nothing from android.*, so this can be compiled and run against a
 * real server on a plain JVM (see tests/jvm_test.sh) instead of only being
 * exercised on a device.
 */
public class ApiClient {

    public static final String DEFAULT_SERVER = "http://piano-puter:8080";
    private static final int TIMEOUT_MS = 15000;

    /** status 0 and network == true means the server could not be reached at all. */
    public static class ApiException extends Exception {
        public final int status;
        public final boolean network;

        ApiException(int status, String message, boolean network) {
            super(message);
            this.status = status;
            this.network = network;
        }
    }

    private String baseUrl;
    private String token;

    public ApiClient(String baseUrl) {
        this.baseUrl = normalize(baseUrl);
    }

    /** Accepts "piano-puter:8080", "http://piano-puter:8080/" and similar. */
    public static String normalize(String raw) {
        String url = raw == null ? "" : raw.trim();
        if (url.isEmpty()) return "";
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    public String getBaseUrl() { return baseUrl; }
    public void setBaseUrl(String url) { this.baseUrl = normalize(url); }
    public String getToken() { return token; }
    public void setToken(String value) { this.token = value == null ? "" : value; }

    // ------------------------------------------------------------- transport

    private Map<String, Object> request(String method, String path, Object body)
            throws ApiException {
        HttpURLConnection connection = null;
        try {
            URL url = new URL(baseUrl + path);
            connection = (HttpURLConnection) url.openConnection();

            // HttpURLConnection throws on PATCH; the server accepts the
            // conventional override header instead.
            boolean patchViaPost = "PATCH".equals(method);
            connection.setRequestMethod(patchViaPost ? "POST" : method);
            if (patchViaPost) {
                connection.setRequestProperty("X-HTTP-Method-Override", "PATCH");
            }

            connection.setConnectTimeout(TIMEOUT_MS);
            connection.setReadTimeout(TIMEOUT_MS);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("User-Agent", "PianoTasks-Android/1.0");
            if (token != null && !token.isEmpty()) {
                connection.setRequestProperty("Authorization", "Bearer " + token);
            }

            if (body != null) {
                byte[] payload = Json.write(body).getBytes("UTF-8");
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(payload.length);
                connection.setRequestProperty("Content-Type", "application/json");
                OutputStream out = connection.getOutputStream();
                try {
                    out.write(payload);
                } finally {
                    out.close();
                }
            }

            int status = connection.getResponseCode();
            InputStream stream = status >= 400
                    ? connection.getErrorStream() : connection.getInputStream();
            String text = stream == null ? "" : readAll(stream);

            if (status >= 400) {
                String message = "HTTP " + status;
                if (!text.isEmpty()) {
                    try {
                        Object error = Json.obj(Json.parse(text)).get("error");
                        if (error != null) message = String.valueOf(error);
                    } catch (RuntimeException ignored) {
                        // keep the generic message
                    }
                }
                throw new ApiException(status, message, false);
            }
            return Json.obj(text.isEmpty() ? null : Json.parse(text));
        } catch (ApiException e) {
            throw e;
        } catch (Exception e) {
            String detail = e.getMessage();
            throw new ApiException(0, e.getClass().getSimpleName()
                    + (detail == null ? "" : ": " + detail), true);
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        byte[] chunk = new byte[8192];
        int read;
        while ((read = in.read(chunk)) != -1) {
            buffer.write(chunk, 0, read);
        }
        in.close();
        return new String(buffer.toByteArray(), "UTF-8");
    }

    // ------------------------------------------------------------- endpoints

    public Map<String, Object> health() throws ApiException {
        return request("GET", "/health", null);
    }

    public Map<String, Object> register(String username, String password, String displayName)
            throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("username", username);
        body.put("password", password);
        if (displayName != null && !displayName.isEmpty()) {
            body.put("display_name", displayName);
        }
        Map<String, Object> result = request("POST", "/api/register", body);
        token = Json.str(result.get("token"));
        return result;
    }

    public Map<String, Object> login(String username, String password) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("username", username);
        body.put("password", password);
        Map<String, Object> result = request("POST", "/api/login", body);
        token = Json.str(result.get("token"));
        return result;
    }

    public void logout() throws ApiException {
        request("POST", "/api/logout", new LinkedHashMap<String, Object>());
        token = "";
    }

    public Map<String, Object> me() throws ApiException {
        return request("GET", "/api/me", null);
    }

    /** Returns {date, server_today, tasks: [...], summary: {...}} */
    public Map<String, Object> listTasks(String day) throws ApiException {
        return listTasks(day, false);
    }

    public Map<String, Object> listTasks(String day, boolean includeArchived) throws ApiException {
        StringBuilder path = new StringBuilder("/api/tasks?archived=");
        path.append(includeArchived ? "1" : "0");
        if (day != null && !day.isEmpty()) {
            path.append("&date=").append(day);
        }
        return request("GET", path.toString(), null);
    }

    public List<Map<String, Object>> tasks(String day) throws ApiException {
        return tasks(day, false);
    }

    public List<Map<String, Object>> tasks(String day, boolean includeArchived)
            throws ApiException {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        for (Object item : Json.arr(listTasks(day, includeArchived).get("tasks"))) {
            out.add(Json.obj(item));
        }
        return out;
    }

    public Map<String, Object> createTask(String title, String notes, String recurrence,
                                          String dueDate) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("title", title);
        body.put("notes", notes == null ? "" : notes);
        body.put("recurrence", recurrence == null ? "none" : recurrence);
        if (dueDate != null && !dueDate.isEmpty()) {
            body.put("due_date", dueDate);
        }
        return request("POST", "/api/tasks", body);
    }

    public Map<String, Object> toggleTask(long id, String day) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        if (day != null && !day.isEmpty()) body.put("date", day);
        return request("POST", "/api/tasks/" + id + "/toggle", body);
    }

    public Map<String, Object> patchTask(long id, Map<String, Object> changes) throws ApiException {
        return request("PATCH", "/api/tasks/" + id, changes);
    }

    public Map<String, Object> renameTask(long id, String title) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("title", title);
        return patchTask(id, body);
    }

    public Map<String, Object> setArchived(long id, boolean archived) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("archived", archived);
        return patchTask(id, body);
    }

    public Map<String, Object> deleteTask(long id) throws ApiException {
        return request("DELETE", "/api/tasks/" + id, null);
    }

    public String exportJson() throws ApiException {
        return Json.write(request("GET", "/api/export", null));
    }
}