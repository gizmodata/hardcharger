/**
 * The drag type the schema browser puts a SQL name under, beside text/plain. The editor takes
 * a drop of this type itself and inserts the name as plain text: Monaco's own drop handling
 * treats dropped text as a snippet.
 */
export const sqlNameDragType = "application/x-hardcharger-sql-name";
