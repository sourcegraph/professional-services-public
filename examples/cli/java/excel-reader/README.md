# Excel Reader Application

A simple Java application that reads Excel files from the current directory and displays their contents in the console.

## Features

- Automatically scans the current directory for Excel files (`.xlsx` and `.xls` formats)
- Reads and displays sheet information, headers, and data
- Handles different cell types (text, numbers, dates, formulas)
- Shows a preview of data (first 5 rows)

## Prerequisites

- Java 17 or higher
- Gradle build tool

## Environment Setup with mise

This project includes a `mise.toml` configuration file for easy environment setup using [mise](https://mise.jdx.dev/).

1. Install mise if you haven't already:
   ```bash
   brew install mise
   ```

2. When you enter the project directory, mise will automatically use:
   - Java Temurin 17
   - Latest Gradle version

3. You can verify the active tools with:
   ```bash
   mise ls
   ```

4. Install tools if needed
   ```bash
   mise install
   ```

## How to Run

1. Clone or download this repository
2. Place your Excel files in the project root directory
3. Open a terminal in the project directory
4. Run the application using Gradle:

```bash
./gradlew run
```

## Project Structure

- `src/main/java/com/example/App.java` - Main application class
- `build.gradle` - Gradle build configuration with Apache POI dependencies

## Dependencies

- Apache POI - For reading Excel files
- Log4j2 - Logging implementation required by Apache POI

## Example Output

```
Excel Reader Application
Found 1 Excel file(s).

Reading file: sample.xlsx
Sheet name: Sheet1

Headers:
Name	Age	Department	Join Date	

Data (first 5 rows):
John Doe	30.0	Engineering	2022-01-15T00:00	
Jane Smith	28.0	Marketing	2021-11-01T00:00	
Bob Johnson	35.0	Finance	2020-05-10T00:00	

Total rows: 3
```
