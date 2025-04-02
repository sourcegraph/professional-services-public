package com.example;

import org.apache.poi.ss.usermodel.*;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;
import org.apache.poi.hssf.usermodel.HSSFWorkbook;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;

public class App {
    public static void main(String[] args) {
        System.out.println("Excel Reader Application");
        
        // Get current directory
        File currentDir = new File(".");
        
        // List all Excel files in current directory
        File[] excelFiles = currentDir.listFiles((dir, name) -> 
            name.endsWith(".xlsx") || name.endsWith(".xls"));
        
        if (excelFiles == null || excelFiles.length == 0) {
            System.out.println("No Excel files found in the current directory.");
            return;
        }
        
        System.out.println("Found " + excelFiles.length + " Excel file(s).");
        
        // Process each Excel file
        for (File file : excelFiles) {
            System.out.println("\nReading file: " + file.getName());
            readExcelFile(file);
        }
    }
    
    private static void readExcelFile(File file) {
        try (FileInputStream fis = new FileInputStream(file);
             Workbook workbook = file.getName().endsWith(".xlsx") ? 
                               new XSSFWorkbook(fis) : new HSSFWorkbook(fis)) {
            
            // Get first sheet
            Sheet sheet = workbook.getSheetAt(0);
            
            // Display sheet name
            System.out.println("Sheet name: " + sheet.getSheetName());
            
            // Print headers (first row)
            Row headerRow = sheet.getRow(0);
            if (headerRow != null) {
                System.out.println("\nHeaders:");
                for (Cell cell : headerRow) {
                    System.out.print(getCellValueAsString(cell) + "\t");
                }
                System.out.println();
            }
            
            // Print data (limited to first 5 rows for brevity)
            System.out.println("\nData (first 5 rows):");
            int rowCount = 0;
            for (Row row : sheet) {
                // Skip header row
                if (row.getRowNum() == 0) continue;
                
                // Print row data
                for (Cell cell : row) {
                    System.out.print(getCellValueAsString(cell) + "\t");
                }
                System.out.println();
                
                // Limit to 5 rows
                if (++rowCount >= 5) break;
            }
            
            System.out.println("\nTotal rows: " + sheet.getLastRowNum());
            
        } catch (IOException e) {
            System.out.println("Error reading Excel file: " + e.getMessage());
        }
    }
    
    private static String getCellValueAsString(Cell cell) {
        if (cell == null) return "";
        
        switch (cell.getCellType()) {
            case STRING:
                return cell.getStringCellValue();
            case NUMERIC:
                if (DateUtil.isCellDateFormatted(cell)) {
                    return cell.getLocalDateTimeCellValue().toString();
                }
                return String.valueOf(cell.getNumericCellValue());
            case BOOLEAN:
                return String.valueOf(cell.getBooleanCellValue());
            case FORMULA:
                return cell.getCellFormula();
            default:
                return "";
        }
    }
}